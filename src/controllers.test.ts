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
  it("appends native dropped paths at scaled coordinates without replacing prior files", () => {
    let start!: () => void;
    const button = { dataset: { start: "resume" }, addEventListener: (_event: string, fn: () => void) => { start = fn; } };
    const zone = { getBoundingClientRect: () => ({ left: 100, top: 100, right: 300, bottom: 200, width: 200, height: 100 }),
      classList: { toggle: vi.fn() } };
    const list = { innerHTML: "" };
    const node = { value: "E:/output", textContent: "", dataset: {}, addEventListener: vi.fn() };
    vi.stubGlobal("document", {
      addEventListener: vi.fn(),
      getElementById: (id: string) => id === "materials-list-resume" ? list : node,
      querySelectorAll: (s: string) => s === "[data-start]" ? [button] : [],
      querySelector: (s: string) => s === '[data-pick="resume"]' ? zone : node,
    });
    vi.stubGlobal("window", { devicePixelRatio: 2, addEventListener: vi.fn() });
    const send = vi.fn(async (_message: Record<string, unknown>) => {});
    const controller = createTaskController(send, { validateSettings: () => "", capabilityEffective: () => true,
      saveFields: vi.fn(), taskProvider: () => ({}) } as any);
    const drop = (paths: string[], x = 400, y = 300) => controller.handleFileDrop({ type: "drop", paths, position: { x, y } } as any);
    drop(["E:/简历.pdf", "E:/证书.png"]);
    drop(["E:/简历.pdf", "E:/说明.txt"]);
    drop(["E:/outside.pdf"], 20, 20);
    start();
    const payload = send.mock.calls[0][0].payload as Record<string, unknown>;
    expect(payload.materials).toEqual(["E:/简历.pdf", "E:/证书.png", "E:/说明.txt"]);
    expect(list.innerHTML).toContain("简历.pdf");
    expect(list.innerHTML).not.toContain("outside.pdf");
    controller.handleFileDrop({ type: "leave" });
    expect(zone.classList.toggle).toHaveBeenLastCalledWith("drag", false);
  });
  it.each(["light", "balanced", "strong"])("sends resume writing style %s as a structured option", (style) => {
    let start!: () => void;
    const button = { dataset: { start: "resume" }, addEventListener: (_event: string, fn: () => void) => { start = fn; } };
    const node = { value: "", textContent: "", dataset: {}, addEventListener: vi.fn() };
    vi.stubGlobal("document", {
      addEventListener: vi.fn(),
      getElementById: () => node,
      querySelectorAll: (selector: string) => selector === "[data-start]" ? [button] : [],
      querySelector: (selector: string) => selector === "#resume-writing-style .chip.on"
        ? { dataset: { v: style } }
        : selector === "#output-dir" ? { value: "E:/test" } : node,
    });
    vi.stubGlobal("window", { addEventListener: vi.fn() });
    const send = vi.fn(async (_message: Record<string, unknown>) => {});
    createTaskController(send, {
      validateSettings: () => "", capabilityEffective: () => true,
      saveFields: vi.fn(), taskProvider: () => ({}),
    } as any);
    start();
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      type: "start_task", payload: expect.objectContaining({ skill_id: "resume_pro", writing_style: style }),
    }));
  });

  it("updates capability badges from the probe and keeps manual toggles available", async () => {
    const fields = new Map<string, any>();
    const field = (id: string) => {
      if (!fields.has(id)) fields.set(id, {
        value: "", hidden: true, style: {}, dataset: {}, options: [],
        classList: { toggle: vi.fn(), remove: vi.fn() },
        addEventListener: vi.fn(),
      });
      return fields.get(id);
    };
    vi.stubGlobal("document", { addEventListener: vi.fn(), querySelector: (s: string) => field(s.slice(1)), getElementById: field });
    vi.stubGlobal("window", {});
    vi.spyOn(settings, "initDb").mockResolvedValue();
    const provider = settings._defaultProvider({ model: "unknown", reasoning_level: "balanced", vision_override: false, tool_calling_override: true });
    field("reasoning-level").options = ["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"].map(value => ({ value }));
    vi.spyOn(settings, "listProviders").mockResolvedValue([provider]);
    vi.spyOn(settings, "loadGlobalSettings").mockResolvedValue({ ...settings.DEFAULT_GLOBAL_SETTINGS, active_provider: provider.id });
    const saved = vi.spyOn(settings, "saveProvider").mockResolvedValue();
    const controller = createProviderController(vi.fn(async () => {}));
    await controller.loadSettings();
    expect(field("reasoning-level").value).toBe("medium");
    const report = settings.emptyProbeReport();
    report.vision.status = "verified";
    report.tool_calling.status = "unsupported";
    controller.handleEvent({ id: "probe-capabilities", event: { type: "capabilities_probed", report } });
    expect(controller.capabilityEffective("vision")).toBe(true);
    expect(controller.capabilityEffective("tool_calling")).toBe(false);
    expect(field("cap-vision").classList.toggle).toHaveBeenLastCalledWith("cap-dim", false);
    expect(saved).toHaveBeenLastCalledWith(expect.objectContaining({ vision_override: null, tool_calling_override: null }));
    field("provider-kind").value = "anthropic";
    field("reasoning-level").value = "xhigh";
    const onKindChange = field("provider-kind").addEventListener.mock.calls.find(([event]: string[]) => event === "change")[1];
    onKindChange();
    expect(field("reasoning-level").value).toBe("auto");
    expect(field("reasoning-level").options.filter((o: any) => !o.disabled).map((o: any) => o.value))
      .toEqual(["auto", "none", "low", "medium", "high"]);
  });

  it("edits the model directly and opens its list only with the arrow", () => {
    const fields = new Map<string, any>();
    const field = (selector: string) => {
      if (!fields.has(selector)) fields.set(selector, {
        value: "", hidden: true, style: {}, dataset: {}, innerHTML: "", textContent: "",
        focus: vi.fn(), handlers: {} as Record<string, (event?: any) => void>,
        addEventListener(event: string, handler: (event?: any) => void) { this.handlers[event] = handler; },
      });
      return fields.get(selector);
    };
    vi.stubGlobal("document", { addEventListener: vi.fn(), querySelector: field, getElementById: () => null });
    vi.stubGlobal("window", {});
    const saved = vi.spyOn(settings, "saveProvider").mockResolvedValue();
    field("#provider-kind").value = "openai_responses";
    field("#reasoning-level").value = "auto";
    const controller = createProviderController(vi.fn(async () => {}));
    field("#model").value = "manual-model";
    field("#model").handlers.input();
    field("#model").handlers.click();
    expect(field("#model-options").hidden).toBe(true);
    expect(saved).toHaveBeenLastCalledWith(expect.objectContaining({ model: "manual-model", kind: "openai_responses" }));
    controller.handleEvent({ id: "models-fetch", event: { type: "models_fetched", models: ["listed-model"] } });
    expect(field("#model").value).toBe("manual-model");
    field("#model-toggle").handlers.click();
    expect(field("#model-options").hidden).toBe(false);
    expect(field("#model-options").innerHTML).toContain("listed-model");
    field("#model-options").handlers.click({ target: { closest: () => ({ dataset: { modelChoice: "listed-model" } }) } });
    expect(field("#model").value).toBe("listed-model");
    expect(field("#model-options").hidden).toBe(true);
    field("#model").value = "";
    field("#model").handlers.input();
    controller.handleEvent({ id: "models-fetch", event: { type: "models_fetched", models: ["listed-model"] } });
    expect(field("#model").value).toBe("");
  });

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
    if (tool !== "resume") {
      expect(send).not.toHaveBeenCalled();
      return;
    }
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
