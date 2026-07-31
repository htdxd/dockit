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
}

