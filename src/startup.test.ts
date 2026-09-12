import { afterEach, expect, it, vi } from "vitest";

const startup = vi.hoisted(() => ({
  listen: vi.fn(), createProviders: vi.fn(), createTasks: vi.fn(),
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: startup.listen }));
vi.mock("./providerController", () => ({ createProviderController: startup.createProviders }));
vi.mock("./taskController", () => ({ createTaskController: startup.createTasks }));

afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks(); });

it("subscribes before enabling navigation-triggered settings and artifact requests", async () => {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
  let subscribed!: (unlisten: () => void) => void;
  startup.listen.mockReturnValue(new Promise((resolve) => { subscribed = resolve; }));
  const providers = { loadSettings: vi.fn(async () => {}), handleEvent: vi.fn() };
  const tasks = { requestArtifactsScan: vi.fn(async () => {}), handleEvent: vi.fn() };
  startup.createProviders.mockReturnValue(providers);
  startup.createTasks.mockReturnValue(tasks);
  const { initializeApp } = await import("./appController");
  const pending = initializeApp();
  expect(startup.createProviders).not.toHaveBeenCalled();
  expect(startup.createTasks).not.toHaveBeenCalled();
  subscribed(() => {});
  await pending;
  expect(providers.loadSettings).toHaveBeenCalledTimes(1);
  expect(tasks.requestArtifactsScan).toHaveBeenCalledTimes(1);
});
