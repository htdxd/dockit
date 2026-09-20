import { open } from "@tauri-apps/plugin-dialog";
import { openPath } from "@tauri-apps/plugin-opener";
import type { SendBackend } from "./backend";
import type { ProviderController } from "./providerController";
import { element } from "./dom";
import { initialTaskState, reduceTaskEvent } from "./state";
import type { BackendEnvelope, TaskState } from "./types";
import { setArtifacts, type ArtifactBuckets } from "./artifacts";
import { escapeHtml, flashSubtab, goSub, renderTaskState, setTaskMeta, toast } from "./ui";

/** 任务输入、进度、取消和成果刷新；供应商设置通过明确接口读取。 */
export function createTaskController(send: SendBackend, settings: ProviderController) {
  const { validateSettings, capabilityEffective } = settings;
  /* ===== 每工具材料 ===== */
  const materialsByTool: Record<string, string[]> = { resume: [] };

  function renderFileRows(tool: string): void {
    const list = document.getElementById(`materials-list-${tool}`);
    if (!list) return;
    list.innerHTML = materialsByTool[tool]
      .map((p) => {
        const name = p.split(/[\\/]/).at(-1) ?? p;
        const ext = p.includes(".") ? (p.split(".").at(-1) ?? "").toUpperCase() : "";
        return `<div class="frow" data-path="${escapeHtml(p)}">📎 ${escapeHtml(name)} <span class="fsz">${escapeHtml(ext)}</span> <span class="fx" data-remove="${tool}">×</span></div>`;
      })
      .join("");
  }

  async function pickMaterials(tool: string): Promise<void> {
    try {
      const selected = await open({ multiple: true, title: "选择材料文件" });
      if (!selected) return;
      const paths = Array.isArray(selected) ? selected : [selected];
      materialsByTool[tool] = [...new Set([...materialsByTool[tool], ...paths])];
    } catch {
      // 非 Tauri 环境：退回原生文件选择（仅浏览器演示，无法提供真实路径）
      const input = document.getElementById(`materials-input-${tool}`) as HTMLInputElement | null;
      if (!input) return;
      input.onchange = () => {
        materialsByTool[tool] = [...new Set([...materialsByTool[tool], ...Array.from(input.files ?? []).map((f) => f.name)])];
        renderFileRows(tool);
      };
      input.click();
      return;
    }
    renderFileRows(tool);
  }

  document.addEventListener("click", (e) => {
    const zone = (e.target as HTMLElement).closest<HTMLElement>("[data-pick]");
    if (zone?.dataset.pick) {
      void pickMaterials(zone.dataset.pick);
      return;
    }
    const rm = (e.target as HTMLElement).closest<HTMLElement>("[data-remove]");
    if (rm?.dataset.remove) {
      const tool = rm.dataset.remove;
      const row = rm.closest<HTMLElement>(".frow");
      const path = row?.dataset.path;
      if (path !== undefined) {
        materialsByTool[tool] = materialsByTool[tool].filter((p) => p !== path);
        renderFileRows(tool);
      }
    }
  });

  // 拖拽到上传区（桌面端仍走点击选择；浏览器演示可直接拖入）
  document.querySelectorAll<HTMLElement>("[data-pick]").forEach((zone) => {
    const tool = zone.dataset.pick!;
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("drag");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("drag");
      const files = e.dataTransfer?.files;
      if (!files?.length) return;
      if ("__TAURI_INTERNALS__" in window) {
        toast("桌面端请点击上传区选择文件", "warn");
        return;
      }
      materialsByTool[tool] = [...new Set([...materialsByTool[tool], ...Array.from(files).map((f) => f.name)])];
      renderFileRows(tool);
    });
  });

  /* ===== 任务 ===== */
  const manualFields = ["basic", "education", "experience", "skills"];
  const manualLabels = ["基本信息", "教育背景", "项目、工作及其他经历", "技能、成果与补充说明"];
  let manualValues = manualFields.map(() => "");
  const manualDialog = document.getElementById("manual-resume-modal");
  document.getElementById("resume-manual")?.addEventListener("click", () => {
    manualFields.forEach((key, i) => {
      (document.getElementById(`manual-${key}`) as HTMLTextAreaElement).value = manualValues[i];
    });
    if (manualDialog) manualDialog.hidden = false;
    document.getElementById("manual-basic")?.focus();
  });
  document.getElementById("manual-cancel")?.addEventListener("click", () => {
    if (manualDialog) manualDialog.hidden = true;
  });
  document.getElementById("manual-save")?.addEventListener("click", () => {
    manualValues = manualFields.map(key => (document.getElementById(`manual-${key}`) as HTMLTextAreaElement).value.trim());
    if (manualDialog) manualDialog.hidden = true;
    const status = document.getElementById("manual-status");
    if (status) status.textContent = manualValues.some(Boolean) ? "已填写，将与上传材料一起提交" : "可与上传材料一起使用";
  });
  manualDialog?.addEventListener("keydown", (event) => {
    if ((event as KeyboardEvent).key === "Escape") manualDialog.hidden = true;
  });
  const SKILLS: Record<string, string> = { resume: "resume_pro" };
  let taskState: TaskState = initialTaskState;
  let taskId = "";
  const backendLogs: string[] = [];

  function setState(next: TaskState): void {
    taskState = next;
    renderTaskState(taskState);
    const active = taskState.status === "running" || taskState.status === "waiting";
    document.querySelectorAll<HTMLButtonElement>("[data-start]").forEach((b) => {
      b.disabled = active;
    });
  }


  function toolPrompt(tool: string): { title: string; prompt: string; templateId?: string; outputFormat?: string; writingStyle?: string } | null {
    const val = (id: string): string =>
      (document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null)?.value.trim() ?? "";
    const chipVal = (id: string): string =>
      document.querySelector<HTMLElement>(`#${id} .chip.on`)?.dataset.v ?? "";
    if (tool === "resume") {
      const role = val("resume-role");
      const tpl = document.querySelector<HTMLElement>("#resume-template .tmpl-card.on")?.dataset.template ?? "t001";
      const extra = val("resume-extra");
      const format = chipVal("resume-format") || "DOCX";
      const length = chipVal("resume-length") || "一页";
      const writingStyle = chipVal("resume-writing-style") || "balanced";
      const lines = [
        `简历模板：${tpl}`,
        role ? `目标岗位：${role}` : "目标岗位待问答确认",
        `输出格式：${format}`,
        `篇幅：${length}`,
      ];
      if (extra) lines.push(`补充要求：${extra}`);
      const manual = manualValues.map((value, i) => value ? `${manualLabels[i]}：\n${value}` : "").filter(Boolean);
      if (manual.length) lines.push("用户手动填写的简历资料：\n" + manual.join("\n\n"));
      const vision = capabilityEffective("vision");
      if (!vision) lines.push("注意：当前模型无视觉能力，将走机械溢出检测流程，不进行视觉版式核验。");
      return { title: role ? `${role} 简历` : "简历制作", prompt: lines.join("\n"), templateId: tpl, outputFormat: format, writingStyle };
    }
    return null;
  }

  document.querySelectorAll<HTMLButtonElement>("[data-start]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tool = btn.dataset.start!;
      if (!SKILLS[tool]) {
        toast("该工具尚未接入后端，后续版本开放", "warn");
        return;
      }
      const error = validateSettings();
      const formError = element<HTMLElement>("#form-error");
      if (error) {
        formError.textContent = error;
        toast("模型配置不完整", "warn");
        return;
      }
      const built = toolPrompt(tool);
      if (!built) return;
      formError.textContent = "";
      backendLogs.length = 0;
      settings.saveFields();
      taskId = crypto.randomUUID();
      setTaskMeta({ tool, title: built.title, taskId });
      // 阶段 7：无 Vision 提示（§12.3）。静态表判断仅供 UI 提示，后端为准。
      const visionOn = capabilityEffective("vision");
      setState({
        ...initialTaskState,
        status: "running",
        progress: ["正在启动本地 Runtime"],
        visionWarning: visionOn
          ? ""
          : "当前模型不支持图像理解。系统仍会解析文字、表格和公式，并仅复用高置信度图片；复杂图片选择和视觉版式检查将被跳过。建议使用支持 Vision 的模型以获得更好的排版质量。",
      });
      goSub(tool, "run");
      flashSubtab(tool, "run");
      toast("任务已开始，正在解析材料…", "info");
      void send({
        id: taskId,
        type: "start_task",
        payload: {
          provider: settings.taskProvider(),
          skill_id: SKILLS[tool],
          user_prompt: built.prompt,
          ...(built.templateId ? { template_id: built.templateId } : {}),
          ...(built.outputFormat ? { output_format: built.outputFormat } : {}),
          ...(built.writingStyle ? { writing_style: built.writingStyle } : {}),
          output_dir: element<HTMLInputElement>("#output-dir").value.trim(),
          materials: materialsByTool[tool],
          // UI 生产路径显式启用领域工具；未带该字段的旧 Sidecar 调用仍走 legacy。
          tool_mode: "domain",
        },
      }).catch((error) => {
        setState(reduceTaskEvent(taskState, { type: "task_failed", error: String(error) }));
      });
    });
  });

  /* 澄清弹窗 */
  /* 澄清弹窗：收集每个问题的回答。select 若选中「✎ 自定义回答…」，
     则用其展开的自由输入框内容作为回答；text/textarea 直接取输入值。 */
  function collectAnswers(): Record<string, string> {
    const answers: Record<string, string> = {};
    document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      "#clarify-questions [data-question-id]",
    ).forEach((field) => {
      const id = field.dataset.questionId ?? "";
      let value: string;
      if (field instanceof HTMLSelectElement && field.value === "__custom__") {
        const custom = Array.from(
          document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("#clarify-questions [data-custom-for]"),
        ).find((el) => el.dataset.customFor === id);
        value = custom?.value ?? "";
      } else {
        value = field.value;
      }
      answers[id] = value;
    });
    return answers;
  }

  element<HTMLButtonElement>("#submit-answers").addEventListener("click", async () => {
    await send({ id: taskId, type: "answer_questions", payload: { answers: collectAnswers() } });
    setState({ ...taskState, status: "running", questions: [] });
  });

  element<HTMLElement>("#clarify-skip").addEventListener("click", async () => {
    await send({ id: taskId, type: "answer_questions", payload: { answers: collectAnswers() } });
    setState({ ...taskState, status: "running", questions: [] });
    toast("已跳过澄清，按当前资料生成", "warn");
  });

  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    // 取消、复制错误与打开成果。
    const cancel = target.closest<HTMLElement>("[data-action='cancel']");
    if (cancel) {
      if (taskId) void send({ id: taskId, type: "cancel_task", payload: {} });
      return;
    }
    const copy = target.closest<HTMLElement>("[data-action='copy-error']");
    if (copy) {
      if (taskState.error) void navigator.clipboard.writeText(taskState.error);
      return;
    }
    const artifact = target.closest<HTMLElement>("[data-artifact-path]");
    if (artifact?.dataset.artifactPath) {
      const path = artifact.dataset.artifactPath;
      if ("__TAURI_INTERNALS__" in window) {
        // 用系统默认应用打开产物（Tauri opener 插件）
        void openPath(path).catch((error) => toast(`打开失败：${String(error)}`, "warn"));
      } else {
        // 浏览器演示：无 opener，提示
        toast("桌面端打开产物（当前为浏览器预览模式）", "info");
      }
      return;
    }
  });

  function handleEvent(payload: BackendEnvelope): void {
    if (payload.id === "system" && payload.event.type === "backend_log") {
      console.error("[Sidecar]", payload.event.message);
      const message = String(payload.event.message);
      backendLogs.push(message);
      if (backendLogs.length > 10) backendLogs.shift();
      if (taskState.status === "running" || taskState.status === "waiting") {
        setState(
          reduceTaskEvent(taskState, {
            type: "debug_log",
            entry: { ts: Date.now() / 1000, phase: "backend_log", content_preview: message },
          }),
        );
      }
      return;
    }
    if (payload.id === "system" && payload.event.type === "backend_transport_error") {
      console.error("[Sidecar]", payload.event.error);
      if (taskState.status === "running" || taskState.status === "waiting") {
        const logs = backendLogs.length ? `\n\n最近后台日志：\n${backendLogs.join("\n")}` : "";
        setState(
          reduceTaskEvent(taskState, { type: "task_failed", error: `${String(payload.event.error)}${logs}` }),
        );
      }
      return;
    }
    /* 产物扫描结果：无论任务是否存在，把已有产物按 skill 归属填入各工具产物页 */
    if (payload.id.startsWith("artifacts-scan-")) {
      if (payload.id !== pendingScan?.id) return;
      pendingScan = undefined;
      if (payload.event.type === "artifacts_listed") {
        const event = payload.event as { by_skill?: ArtifactBuckets };
        setArtifacts(event.by_skill ?? {});
        lastScanAt = Date.now();
      }
      if (refreshAfterScan) {
        refreshAfterScan = false;
        void requestArtifactsScan(true);
      }
      return;
    }
    if (payload.id !== taskId) return;
    setState(reduceTaskEvent(taskState, payload.event));
    if (payload.event.type === "task_completed") void requestArtifactsScan(true);
  }

  /* ===== 产物探查：启动 / 切到产物页时扫描输出目录 ===== */
  const ARTIFACT_FRESH_MS = 5000;
  let scanSequence = 0;
  let scanDirectory = "";
  let lastScanAt = 0;
  let pendingScan: { id: string } | undefined;
  let refreshAfterScan = false;

  /** 启动后扫描输出目录（待 DB 设置加载完再发，确保 output-dir 已填充） */
  async function requestArtifactsScan(force = false): Promise<void> {
    await settings.loadSettings();
    const outputDir = element<HTMLInputElement>("#output-dir").value.trim();
    if (outputDir !== scanDirectory || !outputDir) {
      scanDirectory = outputDir;
      pendingScan = undefined;
      refreshAfterScan = false;
      lastScanAt = 0;
      setArtifacts({});
    }
    if (!outputDir || !("__TAURI_INTERNALS__" in window)) return;
    if (pendingScan) {
      refreshAfterScan ||= force;
      return;
    }
    if (!force && lastScanAt && Date.now() - lastScanAt < ARTIFACT_FRESH_MS) return;
    const id = `artifacts-scan-${++scanSequence}`;
    pendingScan = { id };
    void send({ id, type: "list_artifacts", payload: { output_dir: outputDir } })
      .catch(() => {
        if (pendingScan?.id === id) pendingScan = undefined;
      });
  }

  // 切到某工具「产物」子页时重新扫描，刷新运行中产生的新产物
  window.addEventListener("dockit:artifacts-view-shown", () => {
    void requestArtifactsScan();
  });
  window.addEventListener("dockit:output-directory-changed", () => {
    void requestArtifactsScan();
  });
  renderTaskState(taskState);
  return { handleEvent, requestArtifactsScan };
}
