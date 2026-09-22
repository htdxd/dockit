import type { BackendEvent, QAState, TaskState } from "./types";

export const initialTaskState: TaskState = {
  status: "idle",
  progress: [],
  questions: [],
  artifacts: [],
  error: "",
  debug: [],
  debugLogPath: "",
  materialProgress: [],
  qa: emptyQA(),
  visionWarning: "",
};

export function emptyQA(): QAState {
  return {
    mechanical: "",
    mechanical_issues: [],
    visual: "not_run",
    visual_issues: [],
    repair_rounds: 0,
    used_assets: 0,
    skipped_assets: 0,
  };
}

function append(state: TaskState, message: string): TaskState {
  return { ...state, progress: [...state.progress, message] };
}

export function reduceTaskEvent(state: TaskState, event: BackendEvent): TaskState {
  switch (event.type) {
    case "task_started":
      return append(
        { ...initialTaskState, status: "running", visionWarning: state.visionWarning },
        "任务已启动",
      );
    case "debug_log": {
      const entry = event.entry as TaskState["debug"][number] | undefined;
      if (!entry) return state;
      return { ...state, debug: [...state.debug, entry] };
    }
    case "debug_log_path":
      return { ...state, debugLogPath: String(event.path ?? "") };
    case "model_started":
      return append({ ...state, status: "running" }, `LLM 正在处理（步骤 ${String(event.step)}）`);
    case "model_finished":
      return append(state, `LLM 响应已收到（${String(event.tool_call_count)} 个工具调用）`);
    case "assistant_text":
      return event.text ? append(state, String(event.text)) : state;
    case "tool_started":
      return append(state, `正在执行 ${String(event.tool)}`);
    case "tool_finished":
      return append(state, `${String(event.tool)}：${event.success ? "完成" : "失败"}`);
    case "tool_timeout":
      return append(state, `${String(event.tool)}：执行超时`);
    case "tool_failed":
      return append(state, `${String(event.tool)}：执行异常`);
    case "questions_requested":
      return { ...state, status: "waiting", questions: (event.questions as TaskState["questions"]) ?? [] };
    case "material_progress": {
      const item = {
        path: String(event.path ?? ""),
        phase: String(event.phase ?? ""),
        ...(event.material_id ? { material_id: String(event.material_id) } : {}),
        ...(event.error ? { error: String(event.error) } : {}),
      };
      const key = item.material_id || item.path;
      const rest = state.materialProgress.filter((p) => (p.material_id || p.path) !== key);
      return { ...state, materialProgress: [...rest, item] };
    }
    case "qa_status": {
      const qa = event.qa as Partial<QAState> | undefined;
      return { ...state, qa: { ...emptyQA(), ...(qa ?? {}) } };
    }
    case "task_completed":
      return {
        ...append(state, "产物已生成"),
        status: "completed",
        questions: [],
        artifacts: (event.artifacts as string[]) ?? [],
      };
    case "task_cancelled":
      return append({ ...state, status: "cancelled" }, "任务已取消");
    case "task_failed":
    case "protocol_error":
      return append({ ...state, status: "failed", error: String(event.error ?? "未知错误") }, "任务失败");
    default:
      return state;
  }
}
