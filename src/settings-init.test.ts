import { afterEach, expect, it, vi } from "vitest";

const database = vi.hoisted(() => ({ load: vi.fn() }));
vi.mock("@tauri-apps/plugin-sql", () => ({ default: database }));
afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

it("opens SQLite and performs schema initialization only once", async () => {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
  vi.stubGlobal("localStorage", { getItem: () => null });
  const execute = vi.fn(async () => {});
  const select = vi.fn(async (sql: string) => sql.startsWith("PRAGMA")
    ? [{ name: "reasoning_level" }, { name: "capability_probe" }]
    : [{ c: 1 }]);
  let complete!: (value: unknown) => void;
  database.load.mockReturnValue(new Promise((resolve) => { complete = resolve; }));
  const { initDb } = await import("./settings");
  const first = initDb();
  expect(initDb()).toBe(first);
  complete({ execute, select });
  await first;
  await initDb();
  expect(database.load).toHaveBeenCalledTimes(1);
  expect(execute).toHaveBeenCalledTimes(1);
  expect(select).toHaveBeenCalledTimes(2);
});
