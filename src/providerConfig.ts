/** 供应商配置与能力规则：不访问 DOM、SQLite 或 localStorage。 */


export type ReasoningLevel = "auto" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "fast" | "balanced" | "deep";


export interface CapabilityProbeResult {
  status: "unknown" | "verified" | "unsupported" | "probe_error";
  checked_at: number;
  probe_version: number;
  detail?: string | null;
  control?: "none" | "effort" | "budget" | "adaptive" | null;
  reasoning_level?: ReasoningLevel | null;
}


export interface CapabilityProbeReport {
  tool_calling: CapabilityProbeResult;
  vision: CapabilityProbeResult;
  reasoning_control: CapabilityProbeResult;
  forced_tool_calling?: CapabilityProbeResult;
}


export function emptyProbeResult(): CapabilityProbeResult {
  return { status: "unknown", checked_at: 0, probe_version: 1, detail: null, control: null };
}


export function emptyProbeReport(): CapabilityProbeReport {
  return {
    tool_calling: emptyProbeResult(),
    vision: emptyProbeResult(),
    reasoning_control: emptyProbeResult(),
  };
}


export interface ProviderConfig {
  id: string;
  name: string;
  kind: "openai" | "openai_responses" | "anthropic" | "openai_compatible";
  base_url: string;
  api_key: string;
  model: string;
  model_custom: string;
  /** null = 自动（按模型名静态表判定） */
  vision_override: boolean | null;
  /** null = 默认 true（运行时必需能力） */
  tool_calling_override: boolean | null;
  /** 推理深度：auto=不主动发送 reasoning 参数；保留旧档位用于读取历史配置 */
  reasoning_level: ReasoningLevel;
  /** 版本化能力探测报告（JSON 字符串），未知能力时为空 */
  capability_probe: string;
}


export interface GlobalSettings {
  output_dir: string;
  mineru_key: string;
  active_provider: string;
}


export const DEFAULT_GLOBAL_SETTINGS: GlobalSettings = {
  output_dir: "./outputs",
  mineru_key: "",
  active_provider: "",
};


const PROVIDER_KIND_LABEL: Record<ProviderConfig["kind"], string> = {
  openai: "OpenAI Chat Completions",
  openai_responses: "OpenAI Responses",
  anthropic: "Anthropic",
  openai_compatible: "兼容接入",
};


export function kindLabel(kind: ProviderConfig["kind"]): string {
  return PROVIDER_KIND_LABEL[kind] ?? kind;
}


/* ===== Provider CRUD ===== */

export function defaultProviderName(kind: ProviderConfig["kind"]): string {
  return `${PROVIDER_KIND_LABEL[kind] ?? kind} 供应商`;
}


/** 旧版 localStorage 扁平配置 → 首个供应商卡片（纯函数，可测试） */
export function legacyToProvider(legacy: Record<string, string>): ProviderConfig | null {
  if (!legacy["model"]) return null;
  return _defaultProvider({
    name: "默认供应商",
    kind: legacy["provider-kind"] as ProviderConfig["kind"],
    base_url: legacy["base-url"] ?? "",
    api_key: legacy["api-key"] ?? "",
    model: legacy["model"] ?? "",
    model_custom: legacy["model-custom"] ?? "",
  });
}


export function _defaultProvider(partial: Partial<ProviderConfig>): ProviderConfig {
  const kind = partial.kind ?? "openai_compatible";
  return {
    id: partial.id ?? crypto.randomUUID(),
    name: partial.name ?? defaultProviderName(kind),
    kind,
    base_url: partial.base_url ?? "",
    api_key: partial.api_key ?? "",
    model: partial.model ?? "",
    model_custom: partial.model_custom ?? "",
    vision_override: partial.vision_override ?? null,
    tool_calling_override: partial.tool_calling_override ?? null,
    reasoning_level: partial.reasoning_level ?? "auto",
    capability_probe: partial.capability_probe ?? "",
  };
}


/* ===== 校验（纯逻辑，可测试） ===== */

export function validateProvider(p: ProviderConfig): string {
  if (!p.name.trim()) return "请填写供应商名称。";
  if (!p.model.trim() && !p.model_custom.trim()) return "请填写模型名。";
  if (!p.api_key.trim()) return "请填写 API Key。";
  if (p.kind === "openai_compatible" && !p.base_url.trim()) {
    return "OpenAI-compatible Provider 还需要填写 Base URL。";
  }
  return "";
}


export function effectiveModel(p: ProviderConfig): string {
  return p.model === "__custom" || !p.model ? p.model_custom : p.model;
}


/* ===== 客户端视觉判定：镜像后端 capabilities.py 的静态表（仅用于 UI 提示，后端为准） */
const VISION_MODEL_RE = [
  /gpt-4o/i, /gpt-4\.1/i, /gpt-4\.5/i, /gpt-4-vision/i, /gpt-5/i,
  /gemini/i, /claude-3/i, /claude-4/i,
  /qwen[0-9.\-]*vl/i, /glm-[0-9.]+v/i, /doubao[0-9.\-]*vision/i, /minimax[-_]vl/i,
  /llava/i, /internvl/i, /deepseek-vl/i, /pixtral/i,
];


export function isVisionModel(name: string): boolean {
  return VISION_MODEL_RE.some((re) => re.test(name));
}


/** 从 DB 行解析探测报告；解析失败/为空 → null（显示「未检测」） */
export function parseStoredProbe(raw: string): CapabilityProbeReport | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CapabilityProbeReport;
    if (!parsed?.tool_calling || !parsed?.vision || !parsed?.reasoning_control) return null;
    return parsed;
  } catch {
    return null;
  }
}


/** 探测指纹：kind + base_url + 生效模型（镜像后端 capabilities.probe_fingerprint，
 *  前后端一致，用于校验探测结果是否仍适用于当前供应商）。
 *  手动输入模型（model === "__custom"）时须用 model_custom 参与指纹计算：
 *  发送端（modelName()）与后端都按「生效模型」算指纹，落库端若直接用原始
 *  "__custom" 会得到不同指纹，导致探测结果被误判为「配置已变更」而丢弃。 */
export function probeFingerprint(p: {
  kind: string;
  base_url?: string | null;
  model: string;
  model_custom?: string;
}): string {
  const model = p.model === "__custom" || !p.model ? (p.model_custom ?? "") : p.model;
  return JSON.stringify([p.kind, (p.base_url ?? "").replace(/\/+$/, ""), model]);
}

export function effectiveCapability(provider: ProviderConfig, cap: "tool_calling" | "vision"): boolean {
  const override = cap === "vision" ? provider.vision_override : provider.tool_calling_override;
  if (override !== null && override !== undefined) return override;
  const status = parseStoredProbe(provider.capability_probe)?.[cap].status;
  if (status === "verified") return true;
  if (status === "unsupported") return false;
  return cap === "vision" ? isVisionModel(effectiveModel(provider)) : true;
}

export function modelListIdentity(provider: Pick<ProviderConfig, "kind" | "base_url">): string {
  return JSON.stringify([provider.kind, provider.base_url.replace(/\/+$/, "")]);
}

export function probeIdentity(provider: ProviderConfig): string {
  return JSON.stringify([probeFingerprint(provider), provider.reasoning_level]);
}
