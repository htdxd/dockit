import type { BackendEvent, TaskState } from "./types";

export const initialTaskState: TaskState = {
  status: "idle",
  progress: [],
  questions: [],
  artifacts: [],
  error: "",
  debug: [],
  debugLogPath: "",
};

function append(state: TaskState, message: string): TaskState {
  return { ...state, progress: [...state.progress, message] };
}

export function reduceTaskEvent(state: TaskState, event: BackendEvent): TaskState {
  switch (event.type) {
    case "task_started":
      return append(
        { ...initialTaskState, status: "running" },
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
