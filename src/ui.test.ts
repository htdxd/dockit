import { afterEach, expect, it, vi } from "vitest";
import { initialTaskState } from "./state";

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

it("keeps scanned history when a new task starts or fails without artifacts", async () => {
  const history = { innerHTML: "previously scanned resume.pdf" };
  vi.stubGlobal("document", {
    querySelectorAll: () => [],
    getElementById: (id: string) => id === "art-resume" ? history : null,
  });
  const { renderTaskState, setTaskMeta } = await import("./ui");
  setTaskMeta({ tool: "resume", title: "new task", taskId: "task-2" });
  renderTaskState({ ...initialTaskState, status: "running" });
  expect(history.innerHTML).toBe("previously scanned resume.pdf");
  renderTaskState({ ...initialTaskState, status: "failed", error: "request failed" });
  expect(history.innerHTML).toBe("previously scanned resume.pdf");
});
