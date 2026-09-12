import { afterEach, describe, expect, it, vi } from "vitest";
import { setArtifacts } from "./artifacts";
import { createProviderController } from "./providerController";
import { createTaskController } from "./taskController";
import * as settings from "./settings";
import { setTaskMeta } from "./ui";

vi.mock("./ui", async (importOriginal) => ({
  ...await importOriginal<typeof import("./ui")>(),
  toast: vi.fn(),
  renderTaskState: vi.fn(),
  setModelSummary: vi.fn(),
  setTaskMeta: vi.fn(),
  goSub: vi.fn(),
  flashSubtab: vi.fn(),
}));

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("controller event ownership", () => {
  it.each([["resume", "test"], ["resume", ""], ["docx", "test"], ["ppt", "test"]])("sends structured template selection only for resume (%s, role=%s)", (tool, role) => {
    let start!: () => void;
    const button = { dataset: { start: tool }, addEventListener: (_event: string, callback: () => void) => { start = callback; } };
    const field = { value: "test", textContent: "", addEventListener: vi.fn(), dataset: {} };
    vi.stubGlobal("document", {
      addEventListener: vi.fn(),
      querySelector: (selector: string) => selector === "#resume-template .tmpl-card.on"
        ? { dataset: { template: "t109" } } : field,
      querySelectorAll: (selector: string) => selector === "[data-start]" ? [button] : [],
      getElementById: (id: string) => id === "resume-role" ? { ...field, value: role } : field,
    });
    vi.stubGlobal("window", { addEventListener: vi.fn() });
    const send = vi.fn(async (_message: Record<string, unknown>) => {});
    const providers = createProviderController(send);
    vi.spyOn(providers, "validateSettings").mockReturnValue("");
    vi.spyOn(providers, "capabilityEffective").mockReturnValue(true);
    vi.spyOn(providers, "saveFields").mockImplementation(() => {});
    vi.spyOn(providers, "taskProvider").mockReturnValue({ kind: "mock", model: "mock", api_key: "", base_url: null, vision: true, tool_calling: true, reasoning_level: "auto", capability_probe: "{}" });
    createTaskController(send, providers);
    start();
    const payload = send.mock.calls[0][0].payload as Record<string, unknown>;
    if (tool === "resume") {
      expect(payload.template_id).toBe("t109");
      expect(payload.user_prompt).toContain("简历模板：t109");
      if (!role) {
        expect(payload.user_prompt).toContain("目标岗位待问答确认");
        expect(payload.materials).toEqual([]);
        expect(setTaskMeta).toHaveBeenLastCalledWith(expect.objectContaining({ title: "简历制作" }));
      }
    } else {
      expect(payload).not.toHaveProperty("template_id");
    }
  });

  it("leaves unrelated model and task events for the task controller", () => {
    vi.stubGlobal("document", {
      addEventListener: vi.fn(),
      querySelector: () => ({ addEventListener: vi.fn() }),
      getElementById: () => null,
    });
    const providers = createProviderController(vi.fn(async () => {}));
    expect(providers.handleEvent({ id: "task-1", event: { type: "models_fetched" } })).toBe(false);
    expect(providers.handleEvent({ id: "task-1", event: { type: "task_completed" } })).toBe(false);
    expect(providers.handleEvent({ id: "artifacts-scan", event: { type: "artifacts_listed" } })).toBe(false);
    expect(providers.handleEvent({ id: "mp-models-1", event: { type: "models_fetched" } })).toBe(true);
    expect(providers.handleEvent({ id: "models-fetch", event: { type: "models_fetched", error: "offline" } })).toBe(true);
    expect(providers.handleEvent({ id: "mineru-status-check", event: { type: "mineru_status", ok: true, token_configured: true } })).toBe(true);
  });
});

describe("historical artifacts", () => {
  it("keeps resume ownership and routes retired conversions and unmarked PDFs to documents", () => {
    const hosts: Record<string, { innerHTML: string }> = {
      "art-ppt": { innerHTML: "" }, "art-resume": { innerHTML: "" }, "art-docx": { innerHTML: "" },
    };
    vi.stubGlobal("document", { getElementById: (id: string) => hosts[id] });
    setArtifacts({
      resume: ["candidate.resume_pro.pdf"],
      pdf: ["archive.pdf_docx_routing.docx"],
      unmarked: ["old.PDF", "slides.pptx", "notes.md", "draft<&>.docx"],
    });
    expect(hosts["art-resume"].innerHTML).toContain("candidate.resume_pro.pdf");
    expect(hosts["art-docx"].innerHTML).not.toContain("candidate.resume_pro.pdf");
    expect(hosts["art-docx"].innerHTML).toContain("历史 PDF 转 DOCX（功能已下线）");
    expect(hosts["art-docx"].innerHTML).toContain("old.PDF");
    expect(hosts["art-docx"].innerHTML).toContain("draft&lt;&amp;&gt;.docx");
    expect(hosts["art-docx"].innerHTML).not.toContain("notes.md");
    expect(hosts["art-ppt"].innerHTML).toContain("slides.pptx");
    setArtifacts({});
    expect(Object.values(hosts).every((host) => host.innerHTML === "")).toBe(true);
  });
});

describe("settings and artifact refresh", () => {
  it("loads provider settings once across concurrent and completed calls", async () => {
    const form = { value: "", style: {}, addEventListener: vi.fn(), options: [] };
    vi.stubGlobal("document", {
      addEventListener: vi.fn(), querySelector: () => form, getElementById: () => null,
    });
    vi.stubGlobal("window", {});
    let complete!: () => void;
    const init = vi.spyOn(settings, "initDb").mockReturnValue(new Promise<void>((resolve) => { complete = resolve; }));
    const list = vi.spyOn(settings, "listProviders").mockResolvedValue([settings._defaultProvider({ model: "__custom" })]);
    const globals = vi.spyOn(settings, "loadGlobalSettings").mockResolvedValue({ ...settings.DEFAULT_GLOBAL_SETTINGS });
    const providers = createProviderController(vi.fn(async () => {}));
    const first = providers.loadSettings();
    expect(providers.loadSettings()).toBe(first);
    complete();
    await first;
    await providers.loadSettings();
    expect(init).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(globals).toHaveBeenCalledTimes(1);
  });

  it("coalesces scans, reuses fresh results, and discards old-directory replies", async () => {
    let now = 10000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const output = { value: "E:/first", addEventListener: vi.fn() };
    const hosts: Record<string, { innerHTML: string }> = {
      "art-ppt": { innerHTML: "" }, "art-resume": { innerHTML: "" }, "art-docx": { innerHTML: "" },
    };
    vi.stubGlobal("document", {
      addEventListener: vi.fn(), querySelector: () => output, querySelectorAll: () => [],
      getElementById: (id: string) => hosts[id],
    });
    vi.stubGlobal("window", { __TAURI_INTERNALS__: {}, addEventListener: vi.fn() });
    const send = vi.fn(async (_message: Record<string, unknown>) => {});
    const providers = createProviderController(send);
    vi.spyOn(providers, "loadSettings").mockResolvedValue();
    const tasks = createTaskController(send, providers);
    await Promise.all(Array.from({ length: 10 }, () => tasks.requestArtifactsScan()));
    expect(send).toHaveBeenCalledTimes(1);
    const firstId = String(send.mock.calls[0][0].id);
    tasks.handleEvent({ id: firstId, event: { type: "artifacts_listed", by_skill: { docx: ["first.docx"] } } });
    await tasks.requestArtifactsScan();
    expect(send).toHaveBeenCalledTimes(1);
    expect(hosts["art-docx"].innerHTML).toContain("first.docx");
    now += 5001;
    await tasks.requestArtifactsScan();
    expect(send).toHaveBeenCalledTimes(2);
    const oldId = String(send.mock.calls[1][0].id);
    output.value = "E:/second";
    await tasks.requestArtifactsScan();
    expect(send).toHaveBeenCalledTimes(3);
    expect(hosts["art-docx"].innerHTML).toBe("");
    tasks.handleEvent({ id: oldId, event: { type: "artifacts_listed", by_skill: { docx: ["stale.docx"] } } });
    expect(hosts["art-docx"].innerHTML).toBe("");
    const latestId = String(send.mock.calls[2][0].id);
    tasks.handleEvent({ id: latestId, event: { type: "artifacts_listed", by_skill: { docx: ["second.docx"] } } });
    expect(hosts["art-docx"].innerHTML).toContain("second.docx");
    await tasks.requestArtifactsScan(true);
    expect(send).toHaveBeenCalledTimes(4);
    await tasks.requestArtifactsScan(true);
    expect(send).toHaveBeenCalledTimes(4);
    const pendingId = String(send.mock.calls[3][0].id);
    tasks.handleEvent({ id: pendingId, event: { type: "artifacts_listed", by_skill: {} } });
    await Promise.resolve();
    expect(send).toHaveBeenCalledTimes(5);
    output.value = "";
    await tasks.requestArtifactsScan();
    expect(hosts["art-docx"].innerHTML).toBe("");
    expect(send).toHaveBeenCalledTimes(5);
  });
});
