import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath } from "@tauri-apps/plugin-opener";

import "./style.css";
import { initialTaskState, reduceTaskEvent } from "./state";
import type { BackendEnvelope, TaskState } from "./types";
import { mountLayout, renderTaskState } from "./ui";

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing app root");
mountLayout(root);

function element<T extends HTMLElement>(selector: string): T {
  const found = document.querySelector<T>(selector);
  if (!found) throw new Error(`Missing element: ${selector}`);
  return found;
}

const STORAGE_KEY = "skill-toolbox-form";
const PERSIST_FIELDS = ["provider-kind", "model", "base-url", "api-key", "skill-select", "prompt", "output-dir"] as const;

function loadForm(): void {
  let saved: Record<string, string> = {};
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
  } catch {
    saved = {};
  }
  for (const id of PERSIST_FIELDS) {
    const value = saved[id];
    if (value) {
      const el = document.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`#${id}`);
      if (el) el.value = value;
    }
  }
}

function saveForm(): void {
  const data: Record<string, string> = {};
  for (const id of PERSIST_FIELDS) {
    const el = document.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`#${id}`);
    if (el) data[id] = el.value;
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    /* localStorage may be unavailable; persistence is best-effort */
  }
}

loadForm();
let taskState: TaskState = initialTaskState;
let taskId = "";
const backendLogs: string[] = [];
const startButton = element<HTMLButtonElement>("#start-task");
const cancelButton = element<HTMLButtonElement>("#cancel-task");
const errorLabel = element<HTMLElement>("#form-error");

PERSIST_FIELDS.forEach((id) => {
  const el = document.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`#${id}`);
  el?.addEventListener("change", saveForm);
  el?.addEventListener("blur", saveForm);
});

function setState(next: TaskState): void {
  taskState = next;
  renderTaskState(taskState);
  const active = taskState.status === "running" || taskState.status === "waiting";
  startButton.disabled = active;
  cancelButton.disabled = !active;
}

async function send(message: Record<string, unknown>): Promise<void> {
  await invoke("send_backend_message", { message });
}

const materials: string[] = [];
element<HTMLButtonElement>("#choose-dir").addEventListener("click", async () => {
  const selected = await open({ directory: true, multiple: false, title: "选择输出目录" });
  if (selected) element<HTMLInputElement>("#output-dir").value = selected;
});

element<HTMLButtonElement>("#choose-materials").addEventListener("click", async () => {
  const selected = await open({ multiple: true, title: "选择材料文件" });
  if (!selected) return;
  const paths = Array.isArray(selected) ? selected : [selected];
  materials.length = 0;
  materials.push(...paths);
  element<HTMLInputElement>("#materials-display").value = `${paths.length} 个文件`;
  element<HTMLUListElement>("#materials-list").replaceChildren(
    ...paths.map((p) => {
      const li = document.createElement("li");
      li.textContent = p.split(/[\\/]/).at(-1) ?? p;
      return li;
    }),
  );
});

startButton.addEventListener("click", async () => {
  const kind = element<HTMLSelectElement>("#provider-kind").value;
  const model = element<HTMLInputElement>("#model").value.trim();
  const apiKey = element<HTMLInputElement>("#api-key").value.trim();
  const baseUrl = element<HTMLInputElement>("#base-url").value.trim();
  const outputDir = element<HTMLInputElement>("#output-dir").value.trim();
  if (!model || !apiKey || !outputDir || (kind === "openai_compatible" && !baseUrl)) {
    errorLabel.textContent = "请填写 Model、API Key 和输出目录；兼容 Provider 还需要 Base URL。";
    return;
  }
  errorLabel.textContent = "";
  backendLogs.length = 0;
  saveForm();
  taskId = crypto.randomUUID();
  setState({ ...initialTaskState, status: "running", progress: ["正在启动本地 Runtime"] });
  try {
    const skillId = element<HTMLSelectElement>("#skill-select").value;
    await send({
      id: taskId,
      type: "start_task",
      payload: {
        provider: { kind, model, api_key: apiKey, base_url: baseUrl || null },
        skill_id: skillId,
        user_prompt: element<HTMLTextAreaElement>("#prompt").value.trim(),
        output_dir: outputDir,
        materials,
      },
    });
  } catch (error) {
    setState(reduceTaskEvent(taskState, { type: "task_failed", error: String(error) }));
  }
});

cancelButton.addEventListener("click", async () => {
  if (taskId) await send({ id: taskId, type: "cancel_task", payload: {} });
});

element<HTMLButtonElement>("#submit-answers").addEventListener("click", async () => {
  const answers: Record<string, string> = {};
  document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>("[data-question-id]").forEach((field) => {
    answers[field.dataset.questionId ?? ""] = field.value;
  });
  await send({ id: taskId, type: "answer_questions", payload: { answers } });
  setState({ ...taskState, status: "running", questions: [] });
});

element<HTMLElement>("#artifacts").addEventListener("click", async (event) => {
  const target = (event.target as HTMLElement).closest<HTMLElement>("[data-artifact-path]");
  if (target?.dataset.artifactPath) await openPath(target.dataset.artifactPath);
});

element<HTMLButtonElement>("#copy-error").addEventListener("click", async () => {
  if (taskState.error) await navigator.clipboard.writeText(taskState.error);
});

if ("__TAURI_INTERNALS__" in window) {
  await listen<BackendEnvelope>("backend-event", ({ payload }) => {
    if (payload.id === "system" && payload.event.type === "backend_log") {
      console.error("[Sidecar]", payload.event.message);
      const message = String(payload.event.message);
      backendLogs.push(message);
      if (backendLogs.length > 10) backendLogs.shift();
      if (taskState.status === "running" || taskState.status === "waiting") {
        setState(reduceTaskEvent(taskState, {
          type: "debug_log",
          entry: { ts: Date.now() / 1000, phase: "backend_log", content_preview: message },
        }));
      }
      return;
    }
    if (payload.id === "system" && payload.event.type === "backend_transport_error") {
      console.error("[Sidecar]", payload.event.error);
      if (taskState.status === "running" || taskState.status === "waiting") {
        const logs = backendLogs.length ? `\n\n最近后台日志：\n${backendLogs.join("\n")}` : "";
        setState(reduceTaskEvent(taskState, { type: "task_failed", error: `${String(payload.event.error)}${logs}` }));
      }
      return;
    }
    if (payload.id !== taskId) return;
    setState(reduceTaskEvent(taskState, payload.event));
  });
}

renderTaskState(taskState);
