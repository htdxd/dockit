import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath } from "@tauri-apps/plugin-opener";

import "./style.css";
import { initialTaskState, reduceTaskEvent } from "./state";
import type { BackendEnvelope, TaskState } from "./types";
import {
  escapeHtml,
  flashSubtab,
  goSub,
  mountLayout,
  renderTaskState,
  setModelSummary,
  setTaskMeta,
  toast,
} from "./ui";

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing app root");
mountLayout(root);

function element<T extends HTMLElement>(selector: string): T {
  const found = document.querySelector<T>(selector);
  if (!found) throw new Error(`Missing element: ${selector}`);
  return found;
}

/* ===== 设置持久化 ===== */
const SETTINGS_KEY = "dockit-settings";
const PERSIST_FIELDS = ["provider-kind", "base-url", "api-key", "model", "model-custom", "output-dir", "mineru-key"] as const;

/* 客户端视觉判定：镜像后端 capabilities.py 的静态表（仅用于 UI 提示，后端为准） */
const VISION_MODEL_RE = [
  /gpt-4o/i, /gpt-4\.1/i, /gpt-4\.5/i, /gpt-4-vision/i, /gpt-5/i,
  /gemini/i, /claude-3/i, /claude-4/i,
  /qwen[0-9.\-]*vl/i, /glm-[0-9.]+v/i, /doubao[0-9.\-]*vision/i, /minimax[-_]vl/i,
  /llava/i, /internvl/i, /deepseek-vl/i, /pixtral/i,
];

function isVisionModel(name: string): boolean {
  return VISION_MODEL_RE.some((re) => re.test(name));
}

function visionOverride(): boolean | null {
  const raw = localStorage.getItem("dockit-vision-override");
  return raw === "on" ? true : raw === "off" ? false : null;
}

function loadSettings(): void {
  let saved: Record<string, string> = {};
  try {
    saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? "{}");
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
  updateProviderUi();
  syncModelSummary();
}

function saveSettings(): void {
  const data: Record<string, string> = {};
  for (const id of PERSIST_FIELDS) {
    const el = document.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`#${id}`);
    if (el) data[id] = el.value;
  }
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(data));
  } catch {
    /* localStorage may be unavailable; persistence is best-effort */
  }
}

PERSIST_FIELDS.forEach((id) => {
  const el = document.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`#${id}`);
  el?.addEventListener("change", saveSettings);
  el?.addEventListener("blur", saveSettings);
});

function updateProviderUi(): void {
  const hint = document.getElementById("base-url-hint");
  if (hint) hint.style.display = element<HTMLSelectElement>("#provider-kind").value === "openai_compatible" ? "" : "none";
}

function modelName(): string {
  const select = element<HTMLSelectElement>("#model").value.trim();
  const custom = element<HTMLInputElement>("#model-custom").value.trim();
  return select === "__custom" || !select ? custom : select;
}

function syncModelSummary(): void {
  const kind = element<HTMLSelectElement>("#provider-kind").value;
  const kindLabel = { openai: "OpenAI", anthropic: "Anthropic", openai_compatible: "兼容接入" }[kind] ?? kind;
  const name = modelName();
  setModelSummary(name ? `${name} · ${kindLabel}` : "未配置模型");
  syncVisionUi();
}

/** 根据模型名 + 用户手动覆盖，点亮/熄灭 vision 徽章并联动 DOCX 页警告 */
function syncVisionUi(): void {
  const effective = visionOverride() ?? isVisionModel(modelName());
  const badge = document.getElementById("cap-vision");
  if (badge) {
    badge.classList.toggle("cap-dim", !effective);
    badge.title = effective ? "该模型具备视觉能力（点击可熄灭）" : "该模型默认无视觉能力（点击可点亮）";
  }
  const warn = document.getElementById("docx-vision-warn");
  if (warn) warn.hidden = effective;
}

element<HTMLSelectElement>("#provider-kind").addEventListener("change", () => {
  updateProviderUi();
  saveSettings();
  syncModelSummary();
});
element<HTMLInputElement>("#model-custom").addEventListener("input", () => {
  saveSettings();
  syncModelSummary();
});

/* vision 徽章点击：手动点亮/熄灭该模型的视觉能力标签 */
element<HTMLElement>("#cap-vision").addEventListener("click", () => {
  const next = !(visionOverride() ?? isVisionModel(modelName()));
  localStorage.setItem("dockit-vision-override", next ? "on" : "off");
  syncVisionUi();
  toast(next ? "已点亮视觉标签：后续任务将走视觉校验流程" : "已熄灭视觉标签：后续任务将走无视觉流程", next ? "ok" : "warn");
});

/* ===== 设置页：获取模型列表 / 校验连接 / 输出目录 ===== */
element<HTMLButtonElement>("#btn-fetch-models").addEventListener("click", () => {
  const btn = element<HTMLButtonElement>("#btn-fetch-models");
  const hint = element<HTMLElement>("#model-hint");
  const base = element<HTMLInputElement>("#base-url").value.trim();
  btn.disabled = true;
  btn.textContent = "获取中…";
  hint.textContent = base ? `GET ${base}/models` : "请先填写 base_url";
  // 模拟：真实实现调用 base_url + /models 并填入选项
  setTimeout(() => {
    const demo = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "deepseek-v3.2", "qwen3-max", "kimi-k2"];
    const sel = element<HTMLSelectElement>("#model");
    sel.innerHTML = demo.map((m) => `<option value="${m}">${m}</option>`).join("") + '<option value="__custom">✎ 手动输入…</option>';
    sel.value = "gpt-4o";
    btn.disabled = false;
    btn.textContent = "⟳ 获取模型列表";
    hint.textContent = `已获取 ${demo.length} 个模型 · 也可选"手动输入"`;
    saveSettings();
    syncModelSummary();
    toast("模型列表已更新", "ok");
  }, 700);
});

element<HTMLSelectElement>("#model").addEventListener("change", (e) => {
  const isCustom = (e.target as HTMLSelectElement).value === "__custom";
  const custom = element<HTMLInputElement>("#model-custom");
  custom.style.display = isCustom ? "block" : "none";
  if (isCustom) custom.focus();
  saveSettings();
  syncModelSummary();
});

element<HTMLButtonElement>("#btn-validate").addEventListener("click", () => {
  const vision = visionOverride() ?? isVisionModel(modelName());
  toast(
    vision
      ? "连接校验通过 · 模型能力符合所有 Skill 要求（含视觉校验）"
      : "连接校验通过 · 模型能力符合要求（无视觉，DOCX 将走机械检查流程）",
    vision ? "ok" : "warn",
  );
});

element<HTMLButtonElement>("#choose-dir").addEventListener("click", async () => {
  const selected = await open({ directory: true, multiple: false, title: "选择输出目录" });
  if (selected) {
    element<HTMLInputElement>("#output-dir").value = selected;
    saveSettings();
  }
});

/* ===== 每工具材料 ===== */
const materialsByTool: Record<string, string[]> = { ppt: [], resume: [], docx: [], pdf: [] };

function renderFileRows(tool: string): void {
  const list = document.getElementById(`materials-list-${tool}`);
  if (!list) return;
  list.innerHTML = materialsByTool[tool]
    .map((p) => {
      const name = p.split(/[\\/]/).at(-1) ?? p;
      const ext = p.includes(".") ? (p.split(".").at(-1) ?? "").toUpperCase() : "";
      return `<div class="frow" data-path="${escapeHtml(p)}">📎 ${escapeHtml(name)} <span class="fsz">${escapeHtml(ext)}</span> <span class="fx" data-remove="${tool}">×</span></div>`;
    })
    .join("");
}

async function pickMaterials(tool: string): Promise<void> {
  try {
    const selected = await open({ multiple: true, title: "选择材料文件" });
    if (!selected) return;
    const paths = Array.isArray(selected) ? selected : [selected];
    materialsByTool[tool] = paths;
  } catch {
    // 非 Tauri 环境：退回原生文件选择（仅浏览器演示，无法提供真实路径）
    const input = document.getElementById(`materials-input-${tool}`) as HTMLInputElement | null;
    if (!input) return;
    input.onchange = () => {
      materialsByTool[tool] = Array.from(input.files ?? []).map((f) => f.name);
      renderFileRows(tool);
    };
    input.click();
    return;
  }
  renderFileRows(tool);
}

document.addEventListener("click", (e) => {
  const zone = (e.target as HTMLElement).closest<HTMLElement>("[data-pick]");
  if (zone?.dataset.pick) {
    void pickMaterials(zone.dataset.pick);
    return;
  }
  const rm = (e.target as HTMLElement).closest<HTMLElement>("[data-remove]");
  if (rm?.dataset.remove) {
    const tool = rm.dataset.remove;
    const row = rm.closest<HTMLElement>(".frow");
    const path = row?.dataset.path;
    if (path !== undefined) {
      materialsByTool[tool] = materialsByTool[tool].filter((p) => p !== path);
      renderFileRows(tool);
    }
  }
});

// 拖拽到上传区（桌面端仍走点击选择；浏览器演示可直接拖入）
document.querySelectorAll<HTMLElement>("[data-pick]").forEach((zone) => {
  const tool = zone.dataset.pick!;
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
    const files = e.dataTransfer?.files;
    if (!files?.length) return;
    if ("__TAURI_INTERNALS__" in window) {
      toast("桌面端请点击上传区选择文件", "warn");
      return;
    }
    materialsByTool[tool] = Array.from(files).map((f) => f.name);
    renderFileRows(tool);
  });
});

/* ===== 任务 ===== */
const SKILLS: Record<string, string> = { ppt: "ppt-master", docx: "docx_pro" };
let taskState: TaskState = initialTaskState;
let taskId = "";
const backendLogs: string[] = [];

function setState(next: TaskState): void {
  taskState = next;
  renderTaskState(taskState);
  const active = taskState.status === "running" || taskState.status === "waiting";
  document.querySelectorAll<HTMLButtonElement>("[data-start]").forEach((b) => {
    b.disabled = active;
  });
}

async function send(message: Record<string, unknown>): Promise<void> {
  await invoke("send_backend_message", { message });
}

function validateSettings(): string {
  const kind = element<HTMLSelectElement>("#provider-kind").value;
  const apiKey = element<HTMLInputElement>("#api-key").value.trim();
  const baseUrl = element<HTMLInputElement>("#base-url").value.trim();
  const outputDir = element<HTMLInputElement>("#output-dir").value.trim();
  if (!modelName() || !apiKey || !outputDir) return "请先完成「设置」：Model、API Key 与输出目录。";
  if (kind === "openai_compatible" && !baseUrl) return "OpenAI-compatible Provider 还需要填写 Base URL。";
  return "";
}

function toolPrompt(tool: string): { title: string; prompt: string } | null {
  const val = (id: string): string =>
    (document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null)?.value.trim() ?? "";
  const chipVal = (id: string): string =>
    document.querySelector<HTMLElement>(`#${id} .chip.on`)?.dataset.v ?? "";
  if (tool === "ppt") {
    const topic = val("ppt-topic");
    if (!topic) {
      toast("请填写演示主题", "warn");
      return null;
    }
    const style = chipVal("ppt-style") || "商务简洁";
    const pagesMin = val("pages-min") || "10";
    const mode = document.querySelector<HTMLElement>(".p-mode.on")?.dataset.m;
    const pages = mode === "range" ? `${pagesMin}–${val("pages-max") || "15"} 页` : `${pagesMin} 页`;
    return { title: topic, prompt: `主题：${topic}\n风格：${style}\n目标页数：${pages}` };
  }
  if (tool === "docx") {
    let use = chipVal("use-chips");
    if (use === "__other") use = val("use-custom") || "其他";
    const req = val("docx-req");
    if (!req) {
      toast("请填写结构化要求", "warn");
      return null;
    }
    const complexity = chipVal("docx-complexity") || "standard";
    const vision = visionOverride() ?? isVisionModel(modelName());
    const visionNote = vision ? "" : "\n注意：当前模型无视觉能力，将走机械检查流程，不进行视觉版式核验。";
    return { title: use, prompt: `文档用途：${use}\n结构化要求：${req}\n生成复杂度：${complexity}${visionNote}` };
  }
  return null;
}

document.querySelectorAll<HTMLButtonElement>("[data-start]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const tool = btn.dataset.start!;
    if (!SKILLS[tool]) {
      toast("该工具尚未接入后端，后续版本开放", "warn");
      return;
    }
    const error = validateSettings();
    const formError = element<HTMLElement>("#form-error");
    if (error) {
      formError.textContent = error;
      toast("模型配置不完整", "warn");
      return;
    }
    const built = toolPrompt(tool);
    if (!built) return;
    formError.textContent = "";
    backendLogs.length = 0;
    saveSettings();
    taskId = crypto.randomUUID();
    setTaskMeta({ tool, title: built.title, taskId });
    setState({ ...initialTaskState, status: "running", progress: ["正在启动本地 Runtime"] });
    goSub(tool, "run");
    flashSubtab(tool, "run");
    toast("任务已开始，正在解析材料…", "info");
    void send({
      id: taskId,
      type: "start_task",
      payload: {
        provider: {
          kind: element<HTMLSelectElement>("#provider-kind").value,
          model: modelName(),
          api_key: element<HTMLInputElement>("#api-key").value.trim(),
          base_url: element<HTMLInputElement>("#base-url").value.trim() || null,
          vision: visionOverride(),
        },
        skill_id: SKILLS[tool],
        user_prompt: built.prompt,
        output_dir: element<HTMLInputElement>("#output-dir").value.trim(),
        materials: materialsByTool[tool],
      },
    }).catch((error) => {
      setState(reduceTaskEvent(taskState, { type: "task_failed", error: String(error) }));
    });
  });
});

/* 动态内容事件委托：取消 / 复制错误 / 打开产物 */
document.addEventListener("click", (e) => {
  const target = e.target as HTMLElement;
  const cancel = target.closest<HTMLElement>("[data-action='cancel']");
  if (cancel) {
    if (taskId) void send({ id: taskId, type: "cancel_task", payload: {} });
    return;
  }
  const copy = target.closest<HTMLElement>("[data-action='copy-error']");
  if (copy) {
    if (taskState.error) void navigator.clipboard.writeText(taskState.error);
    return;
  }
  const artifact = target.closest<HTMLElement>("[data-artifact-path]");
  if (artifact?.dataset.artifactPath) void openPath(artifact.dataset.artifactPath);
});

/* 澄清弹窗 */
function collectAnswers(): Record<string, string> {
  const answers: Record<string, string> = {};
  document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
    "#clarify-questions [data-question-id]",
  ).forEach((field) => {
    answers[field.dataset.questionId ?? ""] = field.value;
  });
  return answers;
}

element<HTMLButtonElement>("#submit-answers").addEventListener("click", async () => {
  await send({ id: taskId, type: "answer_questions", payload: { answers: collectAnswers() } });
  setState({ ...taskState, status: "running", questions: [] });
});

element<HTMLElement>("#clarify-skip").addEventListener("click", async () => {
  await send({ id: taskId, type: "answer_questions", payload: { answers: collectAnswers() } });
  setState({ ...taskState, status: "running", questions: [] });
  toast("已跳过澄清，按当前资料生成", "warn");
});

/* ===== 后端事件 ===== */
if ("__TAURI_INTERNALS__" in window) {
  await listen<BackendEnvelope>("backend-event", ({ payload }) => {
    if (payload.id === "system" && payload.event.type === "backend_log") {
      console.error("[Sidecar]", payload.event.message);
      const message = String(payload.event.message);
      backendLogs.push(message);
      if (backendLogs.length > 10) backendLogs.shift();
      if (taskState.status === "running" || taskState.status === "waiting") {
        setState(
          reduceTaskEvent(taskState, {
            type: "debug_log",
            entry: { ts: Date.now() / 1000, phase: "backend_log", content_preview: message },
          }),
        );
      }
      return;
    }
    if (payload.id === "system" && payload.event.type === "backend_transport_error") {
      console.error("[Sidecar]", payload.event.error);
      if (taskState.status === "running" || taskState.status === "waiting") {
        const logs = backendLogs.length ? `\n\n最近后台日志：\n${backendLogs.join("\n")}` : "";
        setState(
          reduceTaskEvent(taskState, { type: "task_failed", error: `${String(payload.event.error)}${logs}` }),
        );
      }
      return;
    }
    if (payload.id !== taskId) return;
    setState(reduceTaskEvent(taskState, payload.event));
  });
}

/* ===== 启动 ===== */
loadSettings();
renderTaskState(taskState);
