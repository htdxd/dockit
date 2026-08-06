import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath } from "@tauri-apps/plugin-opener";

import "./style.css";
import { initialTaskState, reduceTaskEvent } from "./state";
import type { BackendEnvelope, TaskState } from "./types";
import {
  DEFAULT_GLOBAL_SETTINGS,
  defaultProviderName,
  deleteProvider,
  effectiveModel,
  initDb,
  kindLabel,
  listProviders,
  loadGlobalSettings,
  saveGlobalSettings,
  saveProvider,
  _defaultProvider,
  type GlobalSettings,
  type ProviderConfig,
} from "./settings";
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

/* ===== 供应商状态（DB 驱动） ===== */
let providers: ProviderConfig[] = [];
let globalSettings: GlobalSettings = { ...DEFAULT_GLOBAL_SETTINGS };

function activeProvider(): ProviderConfig {
  return providers.find((p) => p.id === globalSettings.active_provider) ?? providers[0] ?? _defaultFormProvider();
}

function _defaultFormProvider(): ProviderConfig {
  const kind = element<HTMLSelectElement>("#provider-kind").value as ProviderConfig["kind"];
  return _defaultProvider({ kind });
}

/* ===== 客户端视觉判定：镜像后端 capabilities.py 的静态表（仅用于 UI 提示，后端为准） */
const VISION_MODEL_RE = [
  /gpt-4o/i, /gpt-4\.1/i, /gpt-4\.5/i, /gpt-4-vision/i, /gpt-5/i,
  /gemini/i, /claude-3/i, /claude-4/i,
  /qwen[0-9.\-]*vl/i, /glm-[0-9.]+v/i, /doubao[0-9.\-]*vision/i, /minimax[-_]vl/i,
  /llava/i, /internvl/i, /deepseek-vl/i, /pixtral/i,
];

function isVisionModel(name: string): boolean {
  return VISION_MODEL_RE.some((re) => re.test(name));
}

/* ===== 表单 ↔ 供应商 ===== */
function formToProvider(): ProviderConfig {
  const current = activeProvider();
  return {
    ...current,
    name: element<HTMLInputElement>("#provider-name").value.trim() || defaultProviderName(current.kind),
    kind: element<HTMLSelectElement>("#provider-kind").value as ProviderConfig["kind"],
    base_url: element<HTMLInputElement>("#base-url").value.trim(),
    api_key: element<HTMLInputElement>("#api-key").value.trim(),
    model: element<HTMLSelectElement>("#model").value,
    model_custom: element<HTMLInputElement>("#model-custom").value.trim(),
  };
}

function applyProviderToForm(p: ProviderConfig): void {
  element<HTMLInputElement>("#provider-name").value = p.name;
  element<HTMLSelectElement>("#provider-kind").value = p.kind;
  element<HTMLInputElement>("#base-url").value = p.base_url;
  element<HTMLInputElement>("#api-key").value = p.api_key;
  element<HTMLSelectElement>("#model").value = p.model;
  element<HTMLInputElement>("#model-custom").value = p.model_custom;
  const custom = element<HTMLInputElement>("#model-custom");
  custom.style.display = p.model === "__custom" ? "block" : "none";
  updateProviderUi();
}

function saveCurrentProvider(): void {
  const provider = formToProvider();
  const idx = providers.findIndex((p) => p.id === provider.id);
  if (idx >= 0) providers[idx] = provider;
  else providers.push(provider);
  void saveProvider(provider);
  renderProviderCards();
  syncModelSummary();
}

async function activateProvider(id: string): Promise<void> {
  const provider = providers.find((p) => p.id === id);
  if (!provider) return;
  globalSettings = { ...globalSettings, active_provider: id };
  await saveGlobalSettings(globalSettings);
  applyProviderToForm(provider);
  renderProviderCards();
  syncModelSummary();
}

async function addProvider(): Promise<void> {
  const provider = _defaultFormProvider();
  providers.push(provider);
  globalSettings = { ...globalSettings, active_provider: provider.id };
  await saveGlobalSettings(globalSettings);
  await saveProvider(provider);
  applyProviderToForm(provider);
  renderProviderCards();
  syncModelSummary();
  toast("已添加供应商卡片，填写配置即可使用", "ok");
}

async function deleteActiveProvider(): Promise<void> {
  const provider = activeProvider();
  if (!provider) return;
  if (!window.confirm(`确定删除供应商「${provider.name}」？删除后不可恢复。`)) return;
  await deleteProvider(provider.id);
  providers = providers.filter((p) => p.id !== provider.id);
  if (!providers.length) {
    const fresh = _defaultFormProvider();
    providers.push(fresh);
    await saveProvider(fresh);
  }
  globalSettings = { ...globalSettings, active_provider: providers[0].id };
  await saveGlobalSettings(globalSettings);
  applyProviderToForm(activeProvider());
  renderProviderCards();
  syncModelSummary();
  toast("供应商已删除", "warn");
}

function renderProviderCards(): void {
  const container = document.getElementById("provider-cards");
  if (!container) return;
  const activeId = globalSettings.active_provider;
  const cards = providers
    .map((p) => {
      const model = effectiveModel(p) || "未填模型";
      const active = p.id === activeId;
      return `<div class="p-card ${active ? "active" : ""}" data-provider-id="${escapeHtml(p.id)}" title="点击切换">
        <div class="p-name">${escapeHtml(p.name)}</div>
        <div class="p-meta">${escapeHtml(kindLabel(p.kind))} · ${escapeHtml(model)}</div>
        ${active ? '<span class="p-badge">当前</span>' : ""}
      </div>`;
    })
    .join("");
  container.innerHTML = `${cards}<div class="p-card add" data-add-provider title="添加新的供应商配置">＋ 添加供应商</div>`;
}

/* ===== 设置加载 / 保存（DB，浏览器降级 localStorage） ===== */
async function loadSettings(): Promise<void> {
  await initDb();
  providers = await listProviders();
  globalSettings = await loadGlobalSettings();

  // 首次启动：没有任何供应商 → 创建默认卡片
  if (!providers.length) {
    const provider = _defaultFormProvider();
    providers.push(provider);
    globalSettings = { ...globalSettings, active_provider: provider.id };
    await saveProvider(provider);
    await saveGlobalSettings(globalSettings);
  }

  const outputDir = element<HTMLInputElement>("#output-dir");
  if (outputDir && globalSettings.output_dir) outputDir.value = globalSettings.output_dir;
  const mineru = element<HTMLInputElement>("#mineru-key");
  if (mineru && globalSettings.mineru_key) mineru.value = globalSettings.mineru_key;

  applyProviderToForm(activeProvider());
  renderProviderCards();
  syncModelSummary();
}

function saveGlobalFields(): void {
  globalSettings = {
    ...globalSettings,
    output_dir: element<HTMLInputElement>("#output-dir").value.trim(),
    mineru_key: element<HTMLInputElement>("#mineru-key").value.trim(),
  };
  void saveGlobalSettings(globalSettings);
}

/* 表单字段变更：写回当前供应商 + 全局字段
   （#provider-kind / #model 已有专用监听器，不在此重复注册，避免双写 DB） */
["#provider-name", "#base-url", "#api-key", "#model-custom", "#mineru-key", "#output-dir"].forEach((selector) => {
  document.querySelector<HTMLElement>(selector)?.addEventListener("change", () => {
    if (selector === "#mineru-key" || selector === "#output-dir") saveGlobalFields();
    else saveCurrentProvider();
  });
});
element<HTMLInputElement>("#model-custom").addEventListener("input", saveCurrentProvider);

function updateProviderUi(): void {
  const hint = document.getElementById("base-url-hint");
  if (hint) hint.style.display = element<HTMLSelectElement>("#provider-kind").value === "openai_compatible" ? "" : "none";
}

function modelName(): string {
  return effectiveModel(activeProvider());
}

function syncModelSummary(): void {
  const name = modelName();
  setModelSummary(name ? `${name} · ${kindLabel(activeProvider().kind)}` : "未配置模型");
  syncCapabilityUi();
}

/** 能力默认值：tool_calling / json_schema 默认开启；vision 按模型静态表判定 */
function capabilityDefault(cap: "tool_calling" | "json_schema" | "vision"): boolean {
  if (cap === "vision") return isVisionModel(modelName());
  return true;
}

function capabilityOverride(cap: "tool_calling" | "json_schema" | "vision"): boolean | null {
  const p = activeProvider();
  if (cap === "vision") return p.vision_override ?? null;
  if (cap === "tool_calling") return p.tool_calling_override ?? null;
  return p.json_schema_override ?? null;
}

function capabilityEffective(cap: "tool_calling" | "json_schema" | "vision"): boolean {
  return capabilityOverride(cap) ?? capabilityDefault(cap);
}

const CAP_IDS: Record<"tool_calling" | "json_schema" | "vision", string> = {
  tool_calling: "cap-tool-calling",
  json_schema: "cap-json-schema",
  vision: "cap-vision",
};

/** 刷新三个能力标签：显示默认值 + 手动覆盖状态；vision 联动 DOCX 页警告 */
function syncCapabilityUi(): void {
  (["tool_calling", "json_schema", "vision"] as const).forEach((cap) => {
    const badge = document.getElementById(CAP_IDS[cap]);
    if (!badge) return;
    const effective = capabilityEffective(cap);
    badge.classList.toggle("cap-dim", !effective);
    const label = cap === "tool_calling" ? "工具调用" : cap === "json_schema" ? "JSON Schema" : "视觉能力";
    const state = capabilityOverride(cap) === null
      ? (effective ? "默认开启" : "默认关闭")
      : (effective ? "已手动开启" : "已手动关闭");
    badge.title = `点击可手动切换${label}（当前：${state}）`;
  });
  const warn = document.getElementById("docx-vision-warn");
  if (warn) warn.hidden = capabilityEffective("vision");
}

element<HTMLSelectElement>("#provider-kind").addEventListener("change", () => {
  updateProviderUi();
  saveCurrentProvider();
});

/* 三个能力标签点击：手动覆盖该能力（写入当前供应商） */
function toggleCapability(cap: "tool_calling" | "json_schema" | "vision"): void {
  const provider = activeProvider();
  const idx = providers.findIndex((p) => p.id === provider.id);
  if (idx < 0) return;
  const next = !capabilityEffective(cap);
  const patch: Partial<ProviderConfig> = { ...provider };
  if (cap === "vision") patch.vision_override = next;
  else if (cap === "tool_calling") patch.tool_calling_override = next;
  else patch.json_schema_override = next;
  providers[idx] = { ...provider, ...patch };
  void saveProvider(providers[idx]);
  syncCapabilityUi();
  const label = cap === "tool_calling" ? "工具调用" : cap === "json_schema" ? "JSON Schema" : "视觉";
  toast(`已${next ? "开启" : "关闭"}${label}标签`, next ? "ok" : "warn");
}

(Object.keys(CAP_IDS) as Array<"tool_calling" | "json_schema" | "vision">).forEach((cap) => {
  element<HTMLElement>(`#${CAP_IDS[cap]}`).addEventListener("click", () => toggleCapability(cap));
});

/* 卡片点击：切换 / 添加 / 删除（事件委托） */
document.addEventListener("click", (e) => {
  const target = e.target as HTMLElement;
  const card = target.closest<HTMLElement>("[data-provider-id]");
  if (card?.dataset.providerId) {
    void activateProvider(card.dataset.providerId);
    return;
  }
  if (target.closest<HTMLElement>("[data-add-provider]")) {
    void addProvider();
    return;
  }
  const del = target.closest<HTMLElement>("#btn-delete-provider");
  if (del) {
    void deleteActiveProvider();
    return;
  }
  // 继续处理既有任务事件（取消 / 复制错误 / 打开产物）
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

/* ===== 设置页：获取模型列表 / 校验连接 / 输出目录 ===== */
const MODELS_REQUEST_ID = "models-fetch";
let modelsFetchTimer: ReturnType<typeof setTimeout> | null = null;

/** 向 sidecar 请求模型列表（Tauri），浏览器演示降级为本地模拟列表 */
function fetchModels(): void {
  const btn = element<HTMLButtonElement>("#btn-fetch-models");
  const hint = element<HTMLElement>("#model-hint");
  const kind = element<HTMLSelectElement>("#provider-kind").value;
  const base = element<HTMLInputElement>("#base-url").value.trim();
  const apiKey = element<HTMLInputElement>("#api-key").value.trim();

  if (!apiKey) {
    hint.textContent = "请先填写 api_key";
    toast("请先填写 api_key", "warn");
    return;
  }
  if (!("__TAURI_INTERNALS__" in window)) {
    // 浏览器演示：无后端可用，保留本地模拟列表
    const demo = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "deepseek-v3.2", "qwen3-max", "kimi-k2"];
    fillModelOptions(demo, demo[0] ?? "");
    hint.textContent = `浏览器演示模式 · 已填 ${demo.length} 个模拟模型`;
    toast("浏览器演示模式：模型列表为本地模拟", "info");
    return;
  }
  btn.disabled = true;
  btn.textContent = "获取中…";
  hint.textContent = base ? `GET ${base}/models` : "正在请求默认端点…";
  void send({
    id: MODELS_REQUEST_ID,
    type: "fetch_models",
    payload: { kind, base_url: base, api_key: apiKey },
  }).catch((error) => {
    hint.textContent = `请求失败：${String(error)}`;
    btn.disabled = false;
    btn.textContent = "⟳ 获取模型列表";
  });
}

/** 将模型列表填入下拉并强制选中第一个（用户确认的交互） */
function fillModelOptions(models: string[], selected: string): void {
  const sel = element<HTMLSelectElement>("#model");
  sel.innerHTML = models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("")
    + '<option value="__custom">✎ 手动输入…</option>';
  sel.value = models.includes(selected) ? selected : (models[0] ?? "");
  const custom = element<HTMLInputElement>("#model-custom");
  custom.style.display = "none";
  saveCurrentProvider();
}

element<HTMLButtonElement>("#btn-fetch-models").addEventListener("click", fetchModels);

/* 填完 api_key / base_url / 切换 Provider 后自动请求模型列表（300ms 防抖） */
function scheduleFetchModels(): void {
  if (modelsFetchTimer) clearTimeout(modelsFetchTimer);
  modelsFetchTimer = setTimeout(() => {
    const apiKey = element<HTMLInputElement>("#api-key").value.trim();
    if (apiKey) fetchModels();
  }, 300);
}
["#api-key", "#base-url", "#provider-kind"].forEach((selector) => {
  document.querySelector<HTMLElement>(selector)?.addEventListener("change", scheduleFetchModels);
});

element<HTMLSelectElement>("#model").addEventListener("change", (e) => {
  const isCustom = (e.target as HTMLSelectElement).value === "__custom";
  const custom = element<HTMLInputElement>("#model-custom");
  custom.style.display = isCustom ? "block" : "none";
  if (isCustom) custom.focus();
  saveCurrentProvider();
});

element<HTMLButtonElement>("#btn-validate").addEventListener("click", () => {
  const vision = capabilityEffective("vision");
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
    saveGlobalFields();
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
    const vision = capabilityEffective("vision");
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
    saveCurrentProvider();
    saveGlobalFields();
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
          vision: capabilityOverride("vision"),
          tool_calling: capabilityOverride("tool_calling"),
          json_schema: capabilityOverride("json_schema"),
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
    /* models_fetched 不关联具体任务，按 request id 处理（设置页模型列表） */
    if (payload.id === MODELS_REQUEST_ID && payload.event.type === "models_fetched") {
      const event = payload.event as { models?: string[]; error?: string };
      const btn = document.getElementById("btn-fetch-models") as HTMLButtonElement | null;
      const hint = document.getElementById("model-hint");
      if (event.error) {
        if (hint) hint.textContent = `获取失败：${event.error}`;
        toast(`模型列表获取失败：${event.error}`, "warn");
      } else if (event.models?.length) {
        fillModelOptions(event.models, event.models[0]);
        if (hint) hint.textContent = `已获取 ${event.models.length} 个模型 · 也可选"手动输入"`;
        toast(`已获取 ${event.models.length} 个模型`, "ok");
      } else {
        if (hint) hint.textContent = "该端点未返回任何模型";
        toast("该端点未返回任何模型，请检查 base_url", "warn");
      }
      if (btn) {
        btn.disabled = false;
        btn.textContent = "⟳ 获取模型列表";
      }
      return;
    }
    if (payload.id !== taskId) return;
    setState(reduceTaskEvent(taskState, payload.event));
  });
}

/* ===== 启动 ===== */
void loadSettings();
renderTaskState(taskState);
