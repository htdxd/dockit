/**
 * 供应商配置持久化层（SQLite via tauri-plugin-sql，浏览器降级 localStorage）。
 *
 * 数据模型：
 * - providers 表：每个供应商一张卡片（kind / base_url / api_key / model /
 *   model_custom / vision_override）。
 * - settings 表（key-value）：output_dir / mineru_key（全局一份）/ active_provider。
 *
 * 非 Tauri 环境（`npm run dev` 浏览器演示）自动降级 localStorage，key 前缀
 * `dockit.` 以与旧版 `dockit-settings` 区分；DB 路径保持与现有代码一致。
 */

import Database from "@tauri-apps/plugin-sql";

export const SETTINGS_KEY = "dockit-settings";
export const VISION_OVERRIDE_KEY = "dockit-vision-override";

export type ReasoningLevel = "auto" | "fast" | "balanced" | "deep";

export interface CapabilityProbeResult {
  status: "unknown" | "verified" | "unsupported" | "probe_error";
  checked_at: number;
  probe_version: number;
  detail?: string | null;
  control?: "none" | "effort" | "budget" | "adaptive" | null;
}

export interface CapabilityProbeReport {
  tool_calling: CapabilityProbeResult;
  vision: CapabilityProbeResult;
  reasoning_control: CapabilityProbeResult;
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
  kind: "openai" | "anthropic" | "openai_compatible";
  base_url: string;
  api_key: string;
  model: string;
  model_custom: string;
  /** null = 自动（按模型名静态表判定） */
  vision_override: boolean | null;
  /** null = 默认 true（运行时必需能力） */
  tool_calling_override: boolean | null;
  /** 生成质量档位：auto=不主动发送 reasoning 参数 */
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
  output_dir: "",
  mineru_key: "",
  active_provider: "",
};

const PROVIDER_KIND_LABEL: Record<ProviderConfig["kind"], string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  openai_compatible: "兼容接入",
};

export function kindLabel(kind: ProviderConfig["kind"]): string {
  return PROVIDER_KIND_LABEL[kind] ?? kind;
}

const TABLE_SQL = `
CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  base_url TEXT DEFAULT '',
  api_key TEXT DEFAULT '',
  model TEXT DEFAULT '',
  model_custom TEXT DEFAULT '',
  vision_override TEXT,
  tool_calling_override TEXT,
  reasoning_level TEXT DEFAULT 'auto',
  capability_probe TEXT DEFAULT '{}',
  created_at INTEGER,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);`;

/** additive migration：旧库补 reasoning_level / capability_probe 列。
 *  旧库中的 json_schema_override 列保留不删、不再读写。 */
const MIGRATION_SQL = [
  "ALTER TABLE providers ADD COLUMN reasoning_level TEXT DEFAULT 'auto'",
  "ALTER TABLE providers ADD COLUMN capability_probe TEXT DEFAULT '{}'",
];

const isTauri = (): boolean => "__TAURI_INTERNALS__" in window;

let _db: Database | null = null;
let _lsFallback = false;

const LS_PREFIX = "dockit.";
const LS_PROVIDERS = `${LS_PREFIX}providers`;
const LS_SETTINGS = `${LS_PREFIX}settings`;

/* ===== 初始化 ===== */

export async function initDb(): Promise<void> {
  if (isTauri()) {
    try {
      _db = await Database.load("sqlite:dockit.db");
      await _db.execute(TABLE_SQL);
      await _ensureColumns();
      await _migrateLegacyLocalStorage();
      return;
    } catch {
      // 插件不可用（如 dev 时未跑 tauri）→ 降级 localStorage
      _db = null;
      _lsFallback = true;
    }
  } else {
    _lsFallback = true;
  }
  await _migrateLegacyLocalStorage();
}

/** additive migration：旧库可能缺 reasoning_level / capability_probe 列。
 *  用 pragma table_info 探测，缺失才 ALTER，兼容新建库与已升级库。 */
async function _ensureColumns(): Promise<void> {
  if (_lsFallback || !_db) return;
  const cols = await _db.select<{ name: string }[]>("PRAGMA table_info(providers)");
  const names = new Set(cols.map((c) => c.name));
  for (const sql of MIGRATION_SQL) {
    const col = /ADD COLUMN (\w+)/.exec(sql)?.[1];
    if (col && !names.has(col)) {
      try {
        await _db.execute(sql);
      } catch {
        /* 并发初始化等场景重复执行可能已建列，忽略 */
      }
    }
  }
}

/* ===== 旧版 localStorage 一次性迁移 ===== */

function _readLegacyFlat(): Record<string, string> {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

async function _migrateLegacyLocalStorage(): Promise<void> {
  const legacy = _readLegacyFlat();
  const provider = legacyToProvider(legacy);
  if (_lsFallback) {
    // 浏览器降级：把旧 flat key 迁移到新 providers/settings 结构
    const existing = _lsGet<ProviderConfig[]>(LS_PROVIDERS, []);
    if (existing.length === 0 && provider) {
      _lsSave(LS_PROVIDERS, [provider]);
      _lsSave(LS_SETTINGS, {
        ...DEFAULT_GLOBAL_SETTINGS,
        output_dir: legacy["output-dir"] ?? "",
        mineru_key: legacy["mineru-key"] ?? "",
      });
    }
    return;
  }
  // SQLite 路径
  const rows = await _db!.select<{ c: number }[]>("SELECT COUNT(*) AS c FROM providers");
  if (rows[0].c > 0) return;
  if (!provider) return; // 没有旧数据，不动
  await _insertProviderRow(provider);
  const visionOverride = localStorage.getItem(VISION_OVERRIDE_KEY);
  if (visionOverride) {
    await _updateProviderRow(provider.id, { vision_override: visionOverride === "on" ? true : false });
  }
  await setSetting("output_dir", legacy["output-dir"] ?? "");
  await setSetting("mineru_key", legacy["mineru-key"] ?? "");
  await setSetting("active_provider", provider.id);
  // 清掉旧 key，避免重复迁移
  try {
    localStorage.removeItem(SETTINGS_KEY);
    localStorage.removeItem(VISION_OVERRIDE_KEY);
  } catch {
    /* best-effort */
  }
}

/* ===== localStorage 降级读写 ===== */

function _lsGet<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function _lsSave(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* best-effort */
  }
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

async function _insertProviderRow(p: ProviderConfig): Promise<void> {
  if (_lsFallback || !_db) {
    const list = _lsGet<ProviderConfig[]>(LS_PROVIDERS, []);
    list.push(p);
    _lsSave(LS_PROVIDERS, list);
    return;
  }
  await _db.execute(
    "INSERT INTO providers (id, name, kind, base_url, api_key, model, model_custom, vision_override, tool_calling_override, reasoning_level, capability_probe, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12)",
    [
      p.id, p.name, p.kind, p.base_url, p.api_key, p.model, p.model_custom,
      _boolToDb(p.vision_override), _boolToDb(p.tool_calling_override),
      p.reasoning_level, p.capability_probe || "{}",
      Date.now(),
    ],
  );
}

async function _updateProviderRow(id: string, patch: Partial<ProviderConfig>): Promise<void> {
  if (_lsFallback || !_db) {
    const list = _lsGet<ProviderConfig[]>(LS_PROVIDERS, []);
    const idx = list.findIndex((p) => p.id === id);
    if (idx >= 0) {
      list[idx] = { ...list[idx], ...patch };
      _lsSave(LS_PROVIDERS, list);
    }
    return;
  }
  const assignments: string[] = [];
  const values: unknown[] = [];
  const fields: (keyof ProviderConfig)[] = [
    "name", "kind", "base_url", "api_key", "model", "model_custom", "reasoning_level",
  ];
  for (const field of fields) {
    if (field in patch) {
      assignments.push(`${field} = $${values.length + 1}`);
      values.push(patch[field]);
    }
  }
  type CapabilityOverrideKey = "vision_override" | "tool_calling_override";
  const overrideFields: CapabilityOverrideKey[] = [
    "vision_override", "tool_calling_override",
  ];
  for (const field of overrideFields) {
    if (field in patch) {
      assignments.push(`${field} = $${values.length + 1}`);
      values.push(_boolToDb(patch[field] ?? null));
    }
  }
  if ("capability_probe" in patch) {
    assignments.push(`capability_probe = $${values.length + 1}`);
    values.push(patch.capability_probe ?? "{}");
  }
  if (!assignments.length) return;
  assignments.push(`updated_at = $${values.length + 1}`);
  values.push(Date.now());
  values.push(id);
  await _db.execute(`UPDATE providers SET ${assignments.join(", ")} WHERE id = $${values.length}`, values);
}

export async function listProviders(): Promise<ProviderConfig[]> {
  if (_lsFallback || !_db) return _lsGet<ProviderConfig[]>(LS_PROVIDERS, []);
  const rows = await _db.select<Array<Record<string, unknown>>>(
    "SELECT * FROM providers ORDER BY created_at ASC",
  );
  return rows.map((row: Record<string, unknown>) => _rowToProvider(row));
}

export async function saveProvider(p: ProviderConfig): Promise<void> {
  if (_lsFallback || !_db) {
    const list = _lsGet<ProviderConfig[]>(LS_PROVIDERS, []);
    const idx = list.findIndex((item) => item.id === p.id);
    if (idx >= 0) list[idx] = p;
    else list.push(p);
    _lsSave(LS_PROVIDERS, list);
    return;
  }
  const rows = await _db.select<{ c: number }[]>(
    "SELECT COUNT(*) AS c FROM providers WHERE id = $1",
    [p.id],
  );
  if (rows[0].c > 0) await _updateProviderRow(p.id, p);
  else await _insertProviderRow(p);
}

export async function deleteProvider(id: string): Promise<void> {
  if (_lsFallback || !_db) {
    const list = _lsGet<ProviderConfig[]>(LS_PROVIDERS, []);
    _lsSave(LS_PROVIDERS, list.filter((p) => p.id !== id));
    return;
  }
  await _db.execute("DELETE FROM providers WHERE id = $1", [id]);
}

/* ===== 全局 settings ===== */

export async function getSetting<T = string>(key: string, fallback: T): Promise<T> {
  if (_lsFallback || !_db) {
    const settings = _lsGet<Record<string, string>>(LS_SETTINGS, {});
    const value = settings[key];
    return value === undefined ? fallback : (value as T);
  }
  const rows = await _db.select<{ value: string }[]>("SELECT value FROM settings WHERE key = $1", [key]);
  return rows.length ? (rows[0].value as T) : fallback;
}

export async function setSetting(key: string, value: string): Promise<void> {
  if (_lsFallback || !_db) {
    const settings = _lsGet<Record<string, string>>(LS_SETTINGS, {});
    settings[key] = value;
    _lsSave(LS_SETTINGS, settings);
    return;
  }
  await _db.execute(
    "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT(key) DO UPDATE SET value = $2",
    [key, value],
  );
}

export async function loadGlobalSettings(): Promise<GlobalSettings> {
  const [output_dir, mineru_key, active_provider] = await Promise.all([
    getSetting("output_dir", ""),
    getSetting("mineru_key", ""),
    getSetting("active_provider", ""),
  ]);
  return { output_dir, mineru_key, active_provider };
}

export async function saveGlobalSettings(settings: GlobalSettings): Promise<void> {
  await Promise.all([
    setSetting("output_dir", settings.output_dir),
    setSetting("mineru_key", settings.mineru_key),
    setSetting("active_provider", settings.active_provider),
  ]);
}

/* ===== 行 ↔ 对象映射 ===== */

function _boolToDb(v: boolean | null): string | null {
  return v === null ? null : v ? "on" : "off";
}

function _boolFromDb(v: unknown): boolean | null {
  if (v === "on") return true;
  if (v === "off") return false;
  return null;
}

function _rowToProvider(row: Record<string, unknown>): ProviderConfig {
  return {
    id: String(row.id),
    name: String(row.name ?? ""),
    kind: (row.kind ?? "openai_compatible") as ProviderConfig["kind"],
    base_url: String(row.base_url ?? ""),
    api_key: String(row.api_key ?? ""),
    model: String(row.model ?? ""),
    model_custom: String(row.model_custom ?? ""),
    vision_override: _boolFromDb(row.vision_override),
    tool_calling_override: _boolFromDb(row.tool_calling_override),
    reasoning_level: ((row.reasoning_level ?? "auto") as ProviderConfig["reasoning_level"]),
    capability_probe: String(row.capability_probe ?? ""),
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
