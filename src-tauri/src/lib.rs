use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;
use tauri::{AppHandle, Emitter, State};

static STARTED: OnceLock<Instant> = OnceLock::new();

// Opt-in local startup measurements; never records settings or task content.
fn startup_trace(stage: &str, detail: &Value) {
    let Ok(path) = std::env::var("DOCKIT_STARTUP_TRACE") else { return };
    let elapsed = STARTED.get_or_init(Instant::now).elapsed().as_secs_f64() * 1000.0;
    let wall_ms = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{}", serde_json::json!({
            "stage": stage, "pid": std::process::id(), "wall_ms": wall_ms,
            "native_ms": elapsed, "detail": detail
        }));
    }
}

#[tauri::command]
fn startup_profile(timings: Value) {
    startup_trace("frontend", &timings);
}

fn parse_backend_line(line: &[u8]) -> serde_json::Result<Value> {
    let text = String::from_utf8_lossy(line);
    serde_json::from_str(&text)
}

fn emit_transport_error(app: &AppHandle, error: String) {
    let _ = app.emit(
        "backend-event",
        serde_json::json!({
            "id": "system",
            "event": {"type": "backend_transport_error", "error": error}
        }),
    );
}

struct BackendProcess {
    child: Child,
    stdin: ChildStdin,
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

#[derive(Default)]
struct BackendState {
    process: Mutex<Option<BackendProcess>>,
}

impl BackendState {
    fn ensure_started(&self, app: &AppHandle) -> Result<(), String> {
        let mut guard = self
            .process
            .lock()
            .map_err(|_| "Backend lock is poisoned")?;
        if guard
            .as_mut()
            .is_some_and(|process| process.child.try_wait().ok().flatten().is_none())
        {
            return Ok(());
        }
        startup_trace("backend_spawn_start", &Value::Null);
        let (program, args, working_dir, python_path) = backend_command()?;
        let mut command = Command::new(program);
        command
            .args(args)
            .current_dir(&working_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("PYTHONUNBUFFERED", "1");
        if let Some(path) = python_path {
            command.env("PYTHONPATH", path);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Failed to start Python Sidecar: {error}"))?;
        startup_trace("backend_spawned", &serde_json::json!({"pid": child.id()}));
        let stdin = child.stdin.take().ok_or("Sidecar stdin is unavailable")?;
        let stdout = child.stdout.take().ok_or("Sidecar stdout is unavailable")?;
        let stderr = child.stderr.take().ok_or("Sidecar stderr is unavailable")?;
        let stdout_app = app.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = Vec::new();
            loop {
                line.clear();
                match reader.read_until(b'\n', &mut line) {
                    Ok(0) => {
                        emit_transport_error(
                            &stdout_app,
                            "Python Sidecar 已停止，任务无法继续".into(),
                        );
                        break;
                    }
                    Err(error) => {
                        emit_transport_error(
                            &stdout_app,
                            format!("读取 Python Sidecar 输出失败: {error}"),
                        );
                        break;
                    }
                    Ok(_) => {}
                }
                match parse_backend_line(&line) {
                    Ok(value) => {
                        let _ = stdout_app.emit("backend-event", value);
                    }
                    Err(error) => {
                        emit_transport_error(
                            &stdout_app,
                            format!("无法解析 Python Sidecar 事件: {error}"),
                        );
                    }
                }
            }
        });
        let stderr_app = app.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stderr);
            let mut line = Vec::new();
            while reader
                .read_until(b'\n', &mut line)
                .is_ok_and(|size| size > 0)
            {
                let message = String::from_utf8_lossy(&line)
                    .trim_end_matches(&['\r', '\n'][..])
                    .to_owned();
                let _ = stderr_app.emit(
                    "backend-event",
                    serde_json::json!({"id":"system","event":{"type":"backend_log","message":message}}),
                );
                line.clear();
            }
        });
        *guard = Some(BackendProcess { child, stdin });
        Ok(())
    }
}

fn backend_command() -> Result<(PathBuf, Vec<String>, PathBuf, Option<PathBuf>), String> {
    if let Some(explicit) = std::env::var_os("SKILL_TOOLBOX_SIDECAR") {
        let path = PathBuf::from(explicit);
        let working_dir = path.parent().unwrap_or(Path::new(".")).to_path_buf();
        return Ok((path, Vec::new(), working_dir, None));
    }
    let project_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("Unable to resolve project root")?
        .to_path_buf();
    let python = project_root
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    if !python.is_file() {
        return Err("Project Python environment is missing; run `uv sync` first".into());
    }
    Ok((
        python,
        vec!["-m".into(), "skill_toolbox.sidecar".into()],
        project_root.clone(),
        Some(project_root.join("backend")),
    ))
}

#[tauri::command]
fn send_backend_message(
    app: AppHandle,
    state: State<'_, BackendState>,
    message: Value,
) -> Result<(), String> {
    state.ensure_started(&app)?;
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "Backend lock is poisoned")?;
    let process = guard.as_mut().ok_or("Backend is unavailable")?;
    let mut line = serde_json::to_vec(&message).map_err(|error| error.to_string())?;
    line.push(b'\n');
    process
        .stdin
        .write_all(&line)
        .map_err(|error| format!("Failed to write to Sidecar: {error}"))?;
    process
        .stdin
        .flush()
        .map_err(|error| format!("Failed to flush Sidecar input: {error}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    STARTED.get_or_init(Instant::now);
    startup_trace("process", &Value::Null);
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_sql::Builder::default().build())
        .manage(BackendState::default())
        .setup(|_| { startup_trace("setup", &Value::Null); Ok(()) })
        .on_page_load(|_, payload| {
            startup_trace("page_load", &serde_json::json!({
                "event": format!("{:?}", payload.event()), "url": payload.url().as_str()
            }));
        })
        .invoke_handler(tauri::generate_handler![send_backend_message, startup_profile])
        .run(tauri::generate_context!())
        .expect("error while running Skill Toolbox");
}

#[cfg(test)]
mod tests {
    use super::parse_backend_line;

    #[test]
    fn backend_line_parser_replaces_invalid_utf8() {
        let mut line = br#"{"id":"x","event":{"type":"assistant_text","text":"bad "#.to_vec();
        line.push(0xff);
        line.extend_from_slice(br#""}}"#);

        let value = parse_backend_line(&line).expect("lossy UTF-8 should remain valid JSON");

        assert_eq!(value["event"]["text"], "bad \u{fffd}");
    }

}
