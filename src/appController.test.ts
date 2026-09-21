import { afterEach, expect, it, vi } from "vitest";

const steps = vi.hoisted(() => [] as string[]);
const drag = vi.hoisted(() => ({ listen: vi.fn(async (_handler: any) => () => {}), handle: vi.fn() }));
vi.mock("@tauri-apps/api/webview", () => ({ getCurrentWebview: () => ({ onDragDropEvent: drag.listen }) }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async () => { steps.push("listen"); }),
}));
vi.mock("./backend", () => ({ sendBackend: vi.fn() }));
vi.mock("./providerController", () => ({
  createProviderController: () => { steps.push("providers"); return ({
    handleEvent: vi.fn(),
    loadSettings: async () => { steps.push("settings"); },
  }); },
}));
vi.mock("./taskController", () => ({
  createTaskController: () => { steps.push("tasks"); return ({
    handleEvent: vi.fn(),
    requestArtifactsScan: async () => { steps.push("artifacts"); },
    handleFileDrop: drag.handle,
  }); },
}));

import { initializeApp } from "./appController";

afterEach(() => { steps.length = 0; vi.unstubAllGlobals(); });

it("registers native events before loading settings and scanning artifacts", async () => {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {}, addEventListener: vi.fn() });
  await initializeApp();
  expect(steps).toEqual(["listen", "providers", "tasks", "settings", "artifacts"]);
  const payload = { type: "drop", paths: ["E:/resume.pdf"], position: { x: 10, y: 20 } };
  drag.listen.mock.calls.at(-1)![0]({ payload });
  expect(drag.handle).toHaveBeenCalledWith(payload);
});

it("loads settings without a native event listener in browser preview", async () => {
  vi.stubGlobal("window", {});
  await initializeApp();
  expect(steps).toEqual(["providers", "tasks", "settings", "artifacts"]);
});
