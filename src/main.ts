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
  type CapabilityProbeReport,
  type CapabilityProbeResult,
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
let mineruReady: boolean | null = null;

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

function renderModelPicker(): void {
  const box = document.getElementById("mp-providers");
  if (!box) return;
  const activeId = globalSettings.active_provider;
  box.innerHTML = providers
    .map((p) => {
      const active = p.id === activeId;
      const model = effectiveModel(p) || "未填模型";
      return `<div class="mp-prov ${active ? "on" : ""}" data-mp-provider="${escapeHtml(p.id)}" title="${escapeHtml(p.base_url || kindLabel(p.kind))}">
        <div class="mp-prov-name">${escapeHtml(p.name)}</div>
        <div class="mp-prov-meta">${escapeHtml(kindLabel(p.kind))} · ${escapeHtml(model)}</div>
      </div>`;
    })
    .join("") || `<div class="mp-empty">暂无供应商，请到设置页添加</div>`;
}

function pickerModelsHost(): HTMLElement | null {
  return document.getElementById("mp-models");
}

function renderPickerModels(): void {
  const host = pickerModelsHost();
  if (!host) return;
  const p = activeProvider();
  host.innerHTML = `<div class="mp-empty">点击左侧供应商获取模型列表</div>`;
  if (p.model === "__custom") {
    host.innerHTML = `<div class="mp-model on" data-mp-model="__custom">✎ ${escapeHtml(p.model_custom || "手动输入")}</div>`;
  }
}

document.addEventListener("click", (e) => {
  const t = e.target as HTMLElement;
  const wm = t.closest<HTMLElement>("#w-model");
  if (wm) {
    const picker = document.getElementById("model-picker");
    if (!picker) return;
    const willOpen = picker.hidden;
    picker.hidden = !willOpen;
    if (willOpen) {
      renderModelPicker();
      renderPickerModels();
    }
    return;
  }
  const prov = t.closest<HTMLElement>("[data-mp-provider]");
  if (prov?.dataset.mpProvider) {
    const id = prov.dataset.mpProvider;
    if (id !== globalSettings.active_provider) {
      void activateProvider(id);
      renderModelPicker();
    }
    void fetchModelsForPicker();
    return;
  }
  const model = t.closest<HTMLElement>("[data-mp-model]");
  if (model?.dataset.mpModel) {
    const value = model.dataset.mpModel;
    const p = activeProvider();
    const patch: ProviderConfig = { ...p, model: value === "__custom" ? "__custom" : value };
    const idx = providers.findIndex((x) => x.id === p.id);
    if (idx >= 0) providers[idx] = patch;
    void saveProvider(patch);
    invalidateProbe();
    applyProviderToForm(patch);
    syncModelSummary();
    renderModelPicker();
    const picker = document.getElementById("model-picker");
    if (picker) picker.hidden = true;
    return;
  }
  const picker = document.getElementById("model-picker");
  if (picker && !picker.hidden && !t.closest("#model-picker")) {
    picker.hidden = true;
  }
});

let pickerModelsRequest = 0;

/** 对当前激活供应商发起一次 fetch_models 请求，结果渲染进二级模型列表 */
function fetchModelsForPicker(): void {
  const host = pickerModelsHost();
  if (!host) return;
  const p = activeProvider();
  const reqId = `mp-models-${++pickerModelsRequest}`;
  host.innerHTML = `<div class="mp-empty">正在获取模型列表…</div>`;
  void send({
    id: reqId,
    type: "fetch_models",
    payload: { kind: p.kind, base_url: p.base_url, api_key: p.api_key },
  }).catch(() => {
    host.innerHTML = `<div class="mp-empty">获取失败，请到设置页检查配置</div>`;
  });
}

/* ===== 表单 ↔ 供应商 ===== */
function formToProvider(): ProviderConfig {
  const current = activeProvider();
  const selModel = element<HTMLSelectElement>("#model").value;
  const customModel = element<HTMLInputElement>("#model-custom").value.trim();
  const reasoningEl = document.getElementById("reasoning-level") as HTMLSelectElement | null;
  return {
    ...current,
    name: element<HTMLInputElement>("#provider-name").value.trim() || defaultProviderName(current.kind),
    kind: element<HTMLSelectElement>("#provider-kind").value as ProviderConfig["kind"],
    base_url: element<HTMLInputElement>("#base-url").value.trim(),
    api_key: element<HTMLInputElement>("#api-key").value.trim(),
    // select 为空时（当前模型不在下拉选项中）回退到内存值，绝不把空写回，
    // 否则 saveCurrentProvider 会把已配置的 model 覆盖成 "" → 卡片变"未配置模型"、
    // start_task 带空 model → 网关 400。
    model: selModel || current.model || "",
    model_custom: customModel || current.model_custom || "",
    reasoning_level: (reasoningEl?.value as ProviderConfig["reasoning_level"]) ?? current.reasoning_level ?? "auto",
  };
}

function applyProviderToForm(p: ProviderConfig): void {
  element<HTMLInputElement>("#provider-name").value = p.name;
  element<HTMLSelectElement>("#provider-kind").value = p.kind;
  element<HTMLInputElement>("#base-url").value = p.base_url;
  element<HTMLInputElement>("#api-key").value = p.api_key;
  const modelSel = element<HTMLSelectElement>("#model");
  // 若当前模型不在下拉选项中，动态补一个 option，避免 select.value 变空后被
  // formToProvider 误读为空并写回。
  const model = p.model === "__custom" ? "" : p.model;
  if (model && !Array.from(modelSel.options).some((o) => o.value === model)) {
    const opt = document.createElement("option");
    opt.value = model;
    opt.textContent = model;
    modelSel.appendChild(opt);
  }
  modelSel.value = p.model;
  element<HTMLInputElement>("#model-custom").value = p.model_custom;
  const custom = element<HTMLInputElement>("#model-custom");
  custom.style.display = p.model === "__custom" ? "block" : "none";
  const reasoning = document.getElementById("reasoning-level") as HTMLSelectElement | null;
  if (reasoning) reasoning.value = p.reasoning_level ?? "auto";
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
  renderProbeStatus(parseStoredProbe(provider.capability_probe));
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
  renderProbeStatus(null);
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
  renderProbeStatus(parseStoredProbe(activeProvider().capability_probe));
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
  void syncMineruKey();

  applyProviderToForm(activeProvider());
  renderProviderCards();
  syncModelSummary();
  // 从 DB 恢复探测结果展示
  const report = parseStoredProbe(activeProvider().capability_probe);
  renderProbeStatus(report);
}

function saveGlobalFields(): void {
  globalSettings = {
    ...globalSettings,
    output_dir: element<HTMLInputElement>("#output-dir").value.trim(),
    mineru_key: element<HTMLInputElement>("#mineru-key").value.trim(),
  };
  void saveGlobalSettings(globalSettings);
}

/* 把 MinerU Token 同步给 sidecar（仅共享材料解析持有，Agent 工具不可见）。
   浏览器演示模式无 sidecar，直接跳过。 */
async function syncMineruKey(): Promise<void> {
  if (!("__TAURI_INTERNALS__" in window)) return;
  const key = element<HTMLInputElement>("#mineru-key").value.trim();
  if (!key) {
    await send({ id: "mineru-key", type: "clear_mineru_key", payload: {} });
    renderMineruStatus();
    return;
  }
  await send({ id: "mineru-key", type: "set_mineru_key", payload: { mineru_key: key } });
  renderMineruStatus();
}

/* 表单字段变更：写回当前供应商 + 全局字段
   （#provider-kind / #model 已有专用监听器，不在此重复注册，避免双写 DB） */
["#provider-name", "#base-url", "#api-key", "#model-custom", "#mineru-key", "#output-dir"].forEach((selector) => {
  document.querySelector<HTMLElement>(selector)?.addEventListener("change", () => {
    if (selector === "#mineru-key" || selector === "#output-dir") {
      saveGlobalFields();
      if (selector === "#mineru-key") void syncMineruKey();
    } else {
      // kind/base_url/model 是探测指纹的一部分：改动即失效
      if (selector === "#base-url") invalidateProbe();
      saveCurrentProvider();
    }
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

/** 能力默认值：tool_calling 默认开启；vision 按模型静态表判定 */
function capabilityDefault(cap: "tool_calling" | "vision"): boolean {
  if (cap === "vision") return isVisionModel(modelName());
  return true;
}

function capabilityOverride(cap: "tool_calling" | "vision"): boolean | null {
  const p = activeProvider();
  if (cap === "vision") return p.vision_override ?? null;
  return p.tool_calling_override ?? null;
}

function capabilityEffective(cap: "tool_calling" | "vision"): boolean {
  return capabilityOverride(cap) ?? capabilityDefault(cap);
}

const CAP_IDS: Record<"tool_calling" | "vision", string> = {
  tool_calling: "cap-tool-calling",
  vision: "cap-vision",
};

/** 刷新能力标签：显示默认值 + 手动覆盖状态；vision 联动 DOCX 页警告。
 *  JSON Schema 能力已移除（§5 数据模型），工具参数 schema 归入 Tool Calling。 */
function syncCapabilityUi(): void {
  (["tool_calling", "vision"] as const).forEach((cap) => {
    const badge = document.getElementById(CAP_IDS[cap]);
    if (!badge) return;
    const effective = capabilityEffective(cap);
    badge.classList.toggle("cap-dim", !effective);
    const label = cap === "tool_calling" ? "工具调用" : "视觉能力";
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
  invalidateProbe();
  saveCurrentProvider();
});

/* 能力标签点击：手动覆盖该能力（写入当前供应商） */
function toggleCapability(cap: "tool_calling" | "vision"): void {
  const provider = activeProvider();
  const idx = providers.findIndex((p) => p.id === provider.id);
  if (idx < 0) return;
  const next = !capabilityEffective(cap);
  const patch: Partial<ProviderConfig> = { ...provider };
  if (cap === "vision") patch.vision_override = next;
  else patch.tool_calling_override = next;
  providers[idx] = { ...provider, ...patch };
  void saveProvider(providers[idx]);
  syncCapabilityUi();
  const label = cap === "tool_calling" ? "工具调用" : "视觉";
  toast(`已${next ? "开启" : "关闭"}${label}标签`, next ? "ok" : "warn");
}

(Object.keys(CAP_IDS) as Array<"tool_calling" | "vision">).forEach((cap) => {
  element<HTMLElement>(`#${CAP_IDS[cap]}`).addEventListener("click", () => toggleCapability(cap));
});

/* 卡片点击：切换 / 添加 / 删除（事件委托） */
document.addEventListener("click", (e) => {
  const target = e.target as HTMLElement;
  const card = target.closest<HTMLElement>("[data-provider-id]");
  if (card?.dataset.providerId) {
    void activateProvider(card.dataset.providerId);
    // 点击设置页供应商卡片 → 切换到「设置」页的「模型供应商」子页
    goSub("settings", "providers");
    return;
  }
  if (target.closest<HTMLElement>("[data-add-provider]")) {
    void addProvider();
    goSub("settings", "providers");
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
  if (artifact?.dataset.artifactPath) {
    const path = artifact.dataset.artifactPath;
    if ("__TAURI_INTERNALS__" in window) {
      // 用系统默认应用打开产物（Tauri opener 插件）
      void openPath(path).catch((error) => toast(`打开失败：${String(error)}`, "warn"));
    } else {
      // 浏览器演示：无 opener，提示
      toast("桌面端打开产物（当前为浏览器预览模式）", "info");
    }
    return;
  }
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

/** 将模型列表填入下拉；优先保留当前生效模型（若在新列表里），
    避免自动获取列表把用户手选的模型顶掉并写回 DB。 */
function fillModelOptions(models: string[], selected: string): void {
  const sel = element<HTMLSelectElement>("#model");
  sel.innerHTML = models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("")
    + '<option value="__custom">✎ 手动输入…</option>';
  const current = effectiveModel(activeProvider());
  sel.value = models.includes(current)
    ? current
    : models.includes(selected) ? selected : (models[0] ?? "");
  // 当前模型不在新列表里 → 动态补 option，避免 select.value 变空
  if (sel.value === "" && current) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = current;
    sel.appendChild(opt);
    sel.value = current;
  }
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
  invalidateProbe();
  saveCurrentProvider();
});

element<HTMLButtonElement>("#btn-save-provider").addEventListener("click", () => {
  saveCurrentProvider();
  saveGlobalFields();
  toast("供应商配置已保存（本地数据库）", "ok");
});

/* ===== 能力探测（真实请求） ===== */
const PROBE_REQUEST_ID = "probe-capabilities";
/** 发送探测时的快照指纹；回包时用它校验，避免过期结果写进别的供应商 */
let probeRequestFingerprint: string | undefined;

/** 从 DB 行解析探测报告；解析失败/为空 → null（显示「未检测」） */
function parseStoredProbe(raw: string): CapabilityProbeReport | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CapabilityProbeReport;
    if (!parsed?.tool_calling || !parsed?.vision || !parsed?.reasoning_control) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** 修改 kind/base_url/model 后立即把探测状态重置为「未检测」。
 *  不自动联网；由用户点击「检测模型能力」或首次任务显式触发。 */
function invalidateProbe(): void {
  const provider = activeProvider();
  const idx = providers.findIndex((p) => p.id === provider.id);
  if (idx < 0) return;
  if (provider.capability_probe) {
    providers[idx] = { ...provider, capability_probe: "" };
    void saveProvider(providers[idx]);
  }
  renderProbeStatus(null);
}

function renderProbeStatus(report: CapabilityProbeReport | null): void {
  const host = document.getElementById("probe-status");
  if (!host) return;
  if (!report) {
    host.textContent = "未检测";
    host.classList.remove("probe-ok", "probe-warn", "probe-err");
    renderMineruStatus();
    return;
  }
  const labels: Array<[string, CapabilityProbeResult]> = [
    ["工具调用", report.tool_calling],
    ["视觉", report.vision],
    ["推理控制", report.reasoning_control],
  ];
  const statusText: Record<string, string> = {
    verified: "✓",
    unsupported: "✗",
    probe_error: "!",
    unknown: "?",
  };
  host.innerHTML = labels
    .map(([label, r]) => `<span class="probe-item" data-status="${r.status}" title="${escapeHtml(r.detail ?? "")}">${label} ${statusText[r.status] ?? "?"}</span>`)
    .join(" · ");
  host.classList.toggle("probe-ok", report.tool_calling.status === "verified");
  host.classList.toggle("probe-warn", report.tool_calling.status === "unknown" || report.tool_calling.status === "probe_error");
  host.classList.toggle("probe-err", report.tool_calling.status === "unsupported");
  renderMineruStatus();
}

/** MinerU 强依赖状态（§12.3）：设置页第三方服务显示 CLI 可解析性。
 *  CLI 入口由后端 resolve_mineru_cli 判定（与任务 preflight 同源）；
 *  结果经 backend-event 的 mineru_status 分支落地，这里只负责发请求。 */
function renderMineruStatus(): void {
  const host = document.getElementById("mineru-status");
  if (!host) return;
  mineruReady = null;
  host.textContent = "MinerU 状态检查中…";
  void send({ id: "mineru-status-check", type: "mineru_status", payload: {} });
}

/** 探测指纹：kind + base_url + 生效模型（镜像后端 capabilities.probe_fingerprint，
 *  前后端一致，用于校验探测结果是否仍适用于当前供应商）。
 *  手动输入模型（model === "__custom"）时须用 model_custom 参与指纹计算：
 *  发送端（modelName()）与后端都按「生效模型」算指纹，落库端若直接用原始
 *  "__custom" 会得到不同指纹，导致探测结果被误判为「配置已变更」而丢弃。 */
function probeFingerprint(p: {
  kind: string;
  base_url?: string | null;
  model: string;
  model_custom?: string;
}): string {
  const model = p.model === "__custom" || !p.model ? (p.model_custom ?? "") : p.model;
  return JSON.stringify([p.kind, (p.base_url ?? "").replace(/\/+$/, ""), model]);
}

/** 把探测报告持久化到当前供应商 —— 仅当 kind/base_url/model 未变（指纹一致）时
 *  才落库。探测期间用户切走/改了供应商时，过期结果直接丢弃，绝不写进别的行。 */
function persistProbeReport(
  report: CapabilityProbeReport,
  requestFingerprint: string | undefined,
): void {
  const provider = activeProvider();
  const currentFingerprint = probeFingerprint(provider);
  if (requestFingerprint !== undefined && requestFingerprint !== currentFingerprint) {
    renderProbeStatus(null);
    toast("模型配置已变更，探测结果已丢弃", "warn");
    // TEMP DEBUG: 记录指纹供定位
    try {
      localStorage.setItem("dockit.probeDebug", JSON.stringify({
        t: Date.now(),
        mismatch: true,
        currentFingerprint,
        requestFingerprint,
        current: { kind: provider.kind, base_url: provider.base_url, model: provider.model, model_custom: provider.model_custom, id: provider.id },
        active_provider: globalSettings.active_provider,
        providers_len: providers.length,
      }));
    } catch { /* ignore */ }
    return;
  }
  try {
    localStorage.setItem("dockit.probeDebug", JSON.stringify({
      t: Date.now(), mismatch: false, currentFingerprint, requestFingerprint,
    }));
  } catch { /* ignore */ }
  const idx = providers.findIndex((p) => p.id === provider.id);
  if (idx < 0) return;
  const patch = { ...provider, capability_probe: JSON.stringify(report) };
  providers[idx] = patch;
  void saveProvider(patch);
}

function probeCapabilities(): void {
  const btn = document.getElementById("btn-probe") as HTMLButtonElement | null;
  if (!btn) return;
  const apiKey = element<HTMLInputElement>("#api-key").value.trim();
  if (!apiKey) {
    toast("请先填写 api_key", "warn");
    return;
  }
  if (!("__TAURI_INTERNALS__" in window)) {
    toast("浏览器演示模式：无法真实探测", "warn");
    return;
  }
  btn.disabled = true;
  btn.textContent = "探测中…";
  const provider = activeProvider();
  // 发送瞬间的快照指纹：回包比对以此为准（与后端 probe_fingerprint 归一化一致）
  probeRequestFingerprint = probeFingerprint({
    kind: element<HTMLSelectElement>("#provider-kind").value,
    base_url: element<HTMLInputElement>("#base-url").value.trim() || null,
    model: modelName(),
  });
  void send({
    id: PROBE_REQUEST_ID,
    type: "probe_capabilities",
    payload: {
      provider: {
        kind: element<HTMLSelectElement>("#provider-kind").value,
        model: modelName(),
        api_key: apiKey,
        base_url: element<HTMLInputElement>("#base-url").value.trim() || null,
        reasoning_level: element<HTMLSelectElement>("#reasoning-level").value,
        capability_probe: provider.capability_probe,
      },
    },
  }).catch((error) => {
    toast(`探测请求失败：${String(error)}`, "warn");
    btn.disabled = false;
    btn.textContent = "🔍 检测模型能力";
  });
}

element<HTMLButtonElement>("#btn-probe").addEventListener("click", probeCapabilities);
element<HTMLSelectElement>("#reasoning-level").addEventListener("change", () => saveCurrentProvider());

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
const SKILLS: Record<string, string> = { ppt: "ppt-master", docx: "docx_pro", pdf: "pdf_docx_routing", resume: "resume_pro" };
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

/** 等待某条请求对应的回包事件（用于 mineru_status 这类查询式消息）。
 *  简化实现：mineru_status 事件由 renderMineruStatus 的专用分支消费，
 *  这里只发请求，结果经事件循环到达（见 listen 的 mineru_status 分支）。 */

function validateSettings(): string {
  const kind = element<HTMLSelectElement>("#provider-kind").value;
  const apiKey = element<HTMLInputElement>("#api-key").value.trim();
  const baseUrl = element<HTMLInputElement>("#base-url").value.trim();
  const outputDir = element<HTMLInputElement>("#output-dir").value.trim();
  if (!modelName() || !apiKey || !outputDir) return "请先完成「设置」：Model、API Key 与输出目录。";
  if (kind === "openai_compatible" && !baseUrl) return "OpenAI-compatible Provider 还需要填写 Base URL。";
  // MinerU 强依赖（实施计划 §3.1）：CLI 与 Token 都必须在任务开始前校验。
  // 这里只做前端即时提示；后端 start_task 仍会二次校验（后端为准）。
  if (!("__TAURI_INTERNALS__" in window)) return "";
  const mineruToken = element<HTMLInputElement>("#mineru-key").value.trim();
  if (!mineruToken) return "MinerU 是强依赖：请先在设置页「第三方服务」填写 API Token（任务开始前会校验）。";
  if (mineruReady !== true) {
    return mineruReady === false
      ? "MinerU CLI 或 Token 当前不可用，请根据状态提示修复后重试。"
      : "MinerU 状态仍在检查，请稍候再开始任务。";
  }
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
  if (tool === "pdf") {
    const pdfs = materialsByTool["pdf"];
    if (!pdfs.length) {
      toast("请先选择要转换的 PDF 文件", "warn");
      return null;
    }
    const vision = capabilityEffective("vision");
    const visionNote = vision ? "" : "\n注意：当前模型无视觉能力，无法进行视觉版式检查，仅报告机械检查结果。";
    return {
      title: `PDF 转 DOCX（${pdfs.length} 个文件）`,
      prompt: `共 ${pdfs.length} 个 PDF 待转换，文件已暂存到工作区 sources/ 下（见材料列表）。${visionNote}`,
    };
  }
  if (tool === "resume") {
    const role = val("resume-role");
    if (!role) {
      toast("请填写目标岗位", "warn");
      return null;
    }
    const tpl = document.querySelector<HTMLElement>("#resume-template .tmpl-card.on")?.dataset.template ?? "t001";
    const extra = val("resume-extra");
    const format = chipVal("resume-format") || "DOCX";
    const length = chipVal("resume-length") || "一页";
    const lines = [
      `简历模板：${tpl}`,
      `目标岗位：${role}`,
      `输出格式：${format}`,
      `篇幅：${length}`,
    ];
    if (extra) lines.push(`补充要求：${extra}`);
    const vision = capabilityEffective("vision");
    if (!vision) lines.push("注意：当前模型无视觉能力，将走机械溢出检测流程，不进行视觉版式核验。");
    return { title: `${role} 简历`, prompt: lines.join("\n") };
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
    // 阶段 7：无 Vision 提示（§12.3）。静态表判断仅供 UI 提示，后端为准。
    const visionOverride = capabilityOverride("vision");
    const visionOn =
      visionOverride === true ||
      (visionOverride === null && isVisionModel(modelName()));
    setState({
      ...initialTaskState,
      status: "running",
      progress: ["正在启动本地 Runtime"],
      visionWarning: visionOn
        ? ""
        : "当前模型不支持图像理解。系统仍会解析文字、表格和公式，并仅复用高置信度图片；复杂图片选择和视觉版式检查将被跳过。建议使用支持 Vision 的模型以获得更好的排版质量。",
    });
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
          vision: visionOverride,
          tool_calling: capabilityOverride("tool_calling"),
          reasoning_level: element<HTMLSelectElement>("#reasoning-level").value,
          capability_probe: activeProvider().capability_probe,
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
/* 澄清弹窗：收集每个问题的回答。select 若选中「✎ 自定义回答…」，
   则用其展开的自由输入框内容作为回答；text/textarea 直接取输入值。 */
function collectAnswers(): Record<string, string> {
  const answers: Record<string, string> = {};
  document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
    "#clarify-questions [data-question-id]",
  ).forEach((field) => {
    const id = field.dataset.questionId ?? "";
    let value: string;
    if (field instanceof HTMLSelectElement && field.value === "__custom__") {
      const custom = Array.from(
        document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("#clarify-questions [data-custom-for]"),
      ).find((el) => el.dataset.customFor === id);
      value = custom?.value ?? "";
    } else {
      value = field.value;
    }
    answers[id] = value;
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
    /* 产物扫描结果：无论任务是否存在，把已有产物按 skill 归属填入各工具产物页 */
    if (payload.id === ARTIFACTS_REQUEST_ID && payload.event.type === "artifacts_listed") {
      const event = payload.event as { files?: string[]; by_skill?: ArtifactBuckets };
      setArtifacts(event.by_skill ?? {});
      return;
    }
    /* models_fetched 按 request id 分发：仅当 id 是模型列表请求时才消费并
       return；其它 id（如任务完成事件）必须继续走下方 task 分发，不能被吞掉。 */
    if (payload.event.type === "models_fetched") {      const event = payload.event as { models?: string[]; error?: string };
      const isSettingsFetch = payload.id === MODELS_REQUEST_ID;
      const isPickerFetch = typeof payload.id === "string" && payload.id.startsWith("mp-models-");
      if (!isSettingsFetch && !isPickerFetch) {
        // 不是模型列表请求 → 不拦截，继续走任务事件分发
      } else if (isSettingsFetch) {
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
      } else {
        const host = pickerModelsHost();
        if (!host) return;
        if (event.error) {
          host.innerHTML = `<div class="mp-empty">获取失败：${escapeHtml(event.error)}</div>`;
        } else if (event.models?.length) {
          const p = activeProvider();
          const current = effectiveModel(p);
          host.innerHTML = event.models
            .map((m) => {
              const on = m === current || (p.model === "__custom" && m === p.model_custom);
              return `<div class="mp-model ${on ? "on" : ""}" data-mp-model="${escapeHtml(m)}" title="${escapeHtml(m)}">${escapeHtml(m)}</div>`;
            })
            .join("")
            + (p.model === "__custom" ? `<div class="mp-model on" data-mp-model="__custom" title="${escapeHtml(p.model_custom || "手动输入")}">✎ ${escapeHtml(p.model_custom || "手动输入")}</div>` : "");
        } else {
          host.innerHTML = `<div class="mp-empty">该端点未返回任何模型，可到设置页手动输入</div>`;
        }
        return;
      }
    }
    if (payload.id === PROBE_REQUEST_ID && payload.event.type === "capabilities_probed") {
      const event = payload.event as {
        report?: CapabilityProbeReport;
        error?: string;
        fingerprint?: string;
      };
      const btn = document.getElementById("btn-probe") as HTMLButtonElement | null;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🔍 检测模型能力";
      }
      if (event.error) {
        renderProbeStatus(null);
        toast(`能力探测失败：${event.error}`, "warn");
      } else if (event.report) {
        // 用发送瞬间的快照指纹校验（而非当前表单值），防止探测期间用户
        // 切走/改了模型后，过期结果写进别的供应商。
        const sentFingerprint = probeRequestFingerprint;
        const serverFingerprint = event.fingerprint;
        const effectiveFingerprint = serverFingerprint ?? sentFingerprint;
        renderProbeStatus(event.report);
        persistProbeReport(event.report, effectiveFingerprint);
        toast("能力探测完成（Tool Calling / Vision / 推理控制）", "ok");
      }
      return;
    }
    if (payload.id === PROBE_REQUEST_ID && payload.event.type === "capabilities_probed") {
      // 已在上方独立分支处理，这里防重复分发
      return;
    }
    if (payload.id === "mineru-status-check" && payload.event.type === "mineru_status") {
      const host = document.getElementById("mineru-status");
      const status = payload.event as { ok?: boolean; token_configured?: boolean };
      const ok = Boolean(status.ok);
      const tokenConfigured = Boolean(status.token_configured);
      mineruReady = ok && tokenConfigured;
      if (host) {
        host.textContent = !ok
          ? "MinerU CLI 未安装 —— 请先 npm install -g mineru-open-api（任务开始前会校验，未装会直接失败）"
          : !tokenConfigured
            ? "MinerU CLI 可用，但未配置 Token —— 请在上方填写 API Token（任务开始前会校验）"
            : "MinerU CLI 与 Token 均已配置 ✓";
        host.classList.toggle("probe-ok", ok && tokenConfigured);
        host.classList.toggle("probe-err", !ok || !tokenConfigured);
      }
      return;
    }
    if (payload.id === ARTIFACTS_REQUEST_ID && payload.event.type === "artifacts_listed") {
      // 产物扫描结果由 1023 行专门分支处理
      return;
    }
    if (payload.id !== taskId) return;
    setState(reduceTaskEvent(taskState, payload.event));
  });
}

/* ===== 产物探查：启动 / 切到产物页时扫描输出目录 ===== */
const ARTIFACTS_REQUEST_ID = "artifacts-scan";

/** 启动后扫描输出目录（待 DB 设置加载完再发，确保 output-dir 已填充） */
async function requestArtifactsScan(): Promise<void> {
  if (!("__TAURI_INTERNALS__" in window)) return;
  await loadSettings();
  const outputDir = element<HTMLInputElement>("#output-dir").value.trim();
  if (!outputDir) return;
  void send({ id: ARTIFACTS_REQUEST_ID, type: "list_artifacts", payload: { output_dir: outputDir } })
    .catch(() => { /* 扫描失败静默，不打扰用户 */ });
}

type ArtifactBuckets = Record<string, string[]>;

/** 纯扩展名分类（用于无 skill 标记的历史产物兜底）。md/txt 归属不明确，不放入任何功能页。 */
function classifyByExt(files: string[]): ArtifactBuckets {
  const buckets: ArtifactBuckets = { ppt: [], resume: [], docx: [], pdf: [] };
  for (const f of files) {
    const name = f.toLowerCase();
    if (name.endsWith(".pptx") || name.endsWith(".ppt")) buckets.ppt.push(f);
    else if (name.endsWith(".docx") || name.endsWith(".doc")) buckets.docx.push(f);
    else if (name.endsWith(".pdf")) buckets.pdf.push(f);
  }
  return buckets;
}

/** 把扫描结果按 skill 归属填入各工具产物页。
 *  by_skill 来自 sidecar _list_artifacts：带标记的文件按产生它的 skill 归位
 * （resume 页从此也会被填充）；unmarked（升级前的历史产物）按扩展名兜底，
 * 不进入简历页。每个文件只归一个桶，杜绝串检/多检。 */
async function setArtifacts(bySkill: ArtifactBuckets): Promise<void> {
  const buckets: ArtifactBuckets = { ppt: [], resume: [], docx: [], pdf: [] };
  buckets.ppt.push(...(bySkill.ppt ?? []));
  buckets.resume.push(...(bySkill.resume ?? []));
  buckets.docx.push(...(bySkill.docx ?? []));
  buckets.pdf.push(...(bySkill.pdf ?? []));
  const fallback = classifyByExt(bySkill.unmarked ?? []);
  buckets.ppt.push(...fallback.ppt);
  buckets.docx.push(...fallback.docx);
  buckets.pdf.push(...fallback.pdf);

  // 按修改时间降序（新产物在前）——list_artifacts 已按 mtime 排序
  for (const tool of Object.keys(buckets) as Array<keyof typeof buckets>) {
    if (!buckets[tool].length) continue;
    const host = document.getElementById(`art-${tool}`);
    if (!host) continue;
    host.innerHTML = buckets[tool]
      .map((p, i) => {
        const ext = (p.split(".").at(-1) ?? "FILE").toUpperCase();
        const name = p.split(/[\\/]/).at(-1) ?? p;
        const badgeCls = ext === "PDF" ? "v-pdf" : "v-docx";
        const badge = ext === "PDF" ? "P" : "W";
        return `<div class="vrow ${i === 0 ? "cur" : ""}">
          <div class="v-badge ${badgeCls}">${badge}</div>
          <div><div class="v-title">${escapeHtml(name)}</div><div class="v-sub">${escapeHtml(p)} · 可在默认专业软件中打开</div></div>
          <div class="v-open" data-artifact-path="${escapeHtml(p)}">打开</div>
        </div>`;
      })
      .join("");
  }
}

/* ===== 启动 ===== */
void requestArtifactsScan();
// 切到某工具「产物」子页时重新扫描，刷新运行中产生的新产物
window.addEventListener("dockit:artifacts-view-shown", () => {
  void requestArtifactsScan();
});
renderTaskState(taskState);
