export type TaskStatus = "idle" | "running" | "waiting" | "completed" | "failed" | "cancelled";

export interface Question {
  id: string;
  label: string;
  type: "text" | "textarea" | "select";
  required?: boolean;
  options?: string[];
}

export interface BackendEvent {
  type: string;
  [key: string]: unknown;
}

export interface BackendEnvelope {
  id: string;
  event: BackendEvent;
}

export interface DebugEntry {
  ts: number;
  phase: string;
  tool?: string;
  success?: boolean;
  error?: string;
  content_preview?: string;
  args_preview?: string;
  assistant_text_preview?: string;
  tool_calls?: Array<{ name: string; args_preview: string }>;
  step?: number;
  skill_id?: string;
  materials_count?: number;
  user_prompt_preview?: string;
  staged?: string[];
}

export interface TaskState {
  status: TaskStatus;
  progress: string[];
  questions: Question[];
  artifacts: string[];
  error: string;
  debug: DebugEntry[];
  debugLogPath: string;
  /** 材料预处理进度（阶段 3+）：path → parsing/done/failed */
  materialProgress: Array<{ path: string; phase: string; error?: string; material_id?: string }>;
  /** 任务级质量状态（阶段 4+）：mechanical/visual 分开呈现，不能合并 */
  qa: QAState;
  /** 无 Vision 警告（阶段 7：开始按钮旁提示） */
  visionWarning: string;
}

export interface QAState {
  mechanical: "passed" | "failed" | "not_run" | "";
  mechanical_issues: string[];
  visual: "passed" | "failed" | "not_run" | "";
  visual_issues: string[];
  repair_rounds: number;
  used_assets: number;
  skipped_assets: number;
}
