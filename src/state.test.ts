import { describe, expect, it } from "vitest";

import { initialTaskState, reduceTaskEvent } from "./state";

describe("reduceTaskEvent", () => {
  it("moves through running, waiting and completed states", () => {
    const running = reduceTaskEvent(initialTaskState, { type: "task_started" });
    const waiting = reduceTaskEvent(running, {
      type: "questions_requested",
      questions: [{ id: "topic", label: "主题", type: "text" }],
    });
    const completed = reduceTaskEvent(waiting, {
      type: "task_completed",
      artifacts: ["D:/output/test.docx"],
    });

    expect(running.status).toBe("running");
    expect(waiting.status).toBe("waiting");
    expect(waiting.questions).toHaveLength(1);
    expect(completed.status).toBe("completed");
    expect(completed.artifacts).toEqual(["D:/output/test.docx"]);
  });

  it("preserves a backend error", () => {
    const failed = reduceTaskEvent(initialTaskState, { type: "task_failed", error: "bad request" });

    expect(failed.status).toBe("failed");
    expect(failed.error).toBe("bad request");
  });

  it("reports tool timeouts without ending the recoverable agent loop", () => {
    const running = { ...initialTaskState, status: "running" as const };
    const timedOut = reduceTaskEvent(running, { type: "tool_timeout", tool: "write" });

    expect(timedOut.status).toBe("running");
    expect(timedOut.progress.at(-1)).toBe("write：执行超时");
  });
});
