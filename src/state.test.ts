import { describe, expect, it } from "vitest";

import { emptyQA, initialTaskState, reduceTaskEvent } from "./state";

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

  it("tracks material preprocessing progress", () => {
    const running = { ...initialTaskState, status: "running" as const };
    const parsing = reduceTaskEvent(running, { type: "material_progress", path: "sources/a.md", phase: "parsing" });
    const done = reduceTaskEvent(parsing, { type: "material_progress", path: "sources/a.md", phase: "done" });

    expect(done.materialProgress).toEqual([{ path: "sources/a.md", phase: "done" }]);
  });

  it("tracks material preprocessing failure with stable code", () => {
    const running = { ...initialTaskState, status: "running" as const };
    const failed = reduceTaskEvent(running, {
      type: "material_progress",
      path: "sources/bad.xyz",
      phase: "failed",
      error: "MATERIAL_UNSUPPORTED",
    });

    expect(failed.materialProgress.at(-1)).toMatchObject({
      path: "sources/bad.xyz",
      phase: "failed",
      error: "MATERIAL_UNSUPPORTED",
    });
  });

  it("separates mechanical and visual QA status", () => {
    const running = { ...initialTaskState, status: "running" as const };
    const qa = reduceTaskEvent(running, {
      type: "qa_status",
      qa: { mechanical: "passed", visual: "not_run", used_assets: 3, skipped_assets: 2 },
    });

    expect(qa.qa.mechanical).toBe("passed");
    // 未执行视觉检查 ≠ 视觉通过
    expect(qa.qa.visual).toBe("not_run");
    expect(qa.qa.used_assets).toBe(3);
    expect(qa.qa.skipped_assets).toBe(2);
  });

  it("starts with visual=not_run QA default", () => {
    expect(initialTaskState.qa.visual).toBe("not_run");
    expect(emptyQA().visual).toBe("not_run");
  });
});

