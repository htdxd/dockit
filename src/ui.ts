import type { DebugEntry, Question, TaskState } from "./types";

export function mountLayout(root: HTMLElement): void {
  root.innerHTML = `
    <main class="shell">
      <header class="hero">
        <div><span class="eyebrow">SKILL TOOLBOX</span><h1>把要求变成可编辑的文件</h1></div>
        <span class="status-dot" title="本地任务 Runtime"></span>
      </header>
      <section class="skill-card">
        <div class="skill-icon">W</div>
        <div><h2>简易 DOCX 测试</h2><p>生成包含表格、公式和代码块的可编辑 Word 文档。</p></div>
        <span class="tag">内置</span>
      </section>
      <div class="grid">
        <section class="panel">
          <h3>模型连接</h3>
          <label>Provider<select id="provider-kind"><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="openai_compatible">OpenAI-compatible</option></select></label>
          <label>Model<input id="model" placeholder="输入模型名称" autocomplete="off" /></label>
          <label>Base URL（可选）<input id="base-url" placeholder="https://api.example.com/v1" autocomplete="off" /></label>
          <label>API Key<input id="api-key" type="password" placeholder="仅在内存中使用" autocomplete="off" /></label>
        </section>
        <section class="panel">
          <h3>任务</h3>
          <label>工具<select id="skill-select"><option value="simple_docx">简易 DOCX</option><option value="ppt-master">PPT 生成</option></select></label>
          <label>要求说明<textarea id="prompt" rows="6" placeholder="例如：以机器学习项目为主题 / 做 10 页季度汇报"></textarea></label>
          <label>材料文件（可选）<div class="path-row"><input id="materials-display" readonly placeholder="模板 PPTX / 参考文档" /><button id="choose-materials" class="secondary">选择</button></div></label>
          <ul id="materials-list" class="materials-list muted"></ul>
          <label>输出目录<div class="path-row"><input id="output-dir" readonly placeholder="请选择目录" /><button id="choose-dir" class="secondary">选择</button></div></label>
          <div class="actions"><button id="start-task" class="primary">生成</button><button id="cancel-task" class="secondary" disabled>取消</button></div>
          <p id="form-error" class="error" role="alert"></p>
        </section>
      </div>
      <section id="questions-panel" class="panel hidden"><h3>需要补充的信息</h3><form id="questions-form"></form><button id="submit-answers" class="primary">继续生成</button></section>
      <section class="panel result-panel"><div class="result-head"><h3>任务状态</h3><span id="task-status" class="status-pill">未开始</span></div><ol id="progress" class="progress"><li class="muted">生成后会在这里显示进度</li></ol><div id="task-error" class="task-error hidden"><code id="task-error-text"></code><button id="copy-error" class="secondary">复制错误</button></div><div id="artifacts" class="artifacts"></div></section>
      <section class="panel">
        <details id="debug-details" class="debug-details">
          <summary>运行日志 <span id="debug-count" class="muted">(0)</span></summary>
          <p id="debug-log-path" class="debug-path muted"></p>
          <ol id="debug-list" class="debug-list"></ol>
        </details>
      </section>
    </main>`;
}

function questionField(question: Question): HTMLLabelElement {
  const label = document.createElement("label");
  label.append(document.createTextNode(question.label));
  let field: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
  if (question.type === "textarea") {
    field = document.createElement("textarea");
  } else if (question.type === "select") {
    field = document.createElement("select");
    field.append(
      ...(question.options ?? []).map((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        return option;
      }),
    );
  } else {
    field = document.createElement("input");
  }
  field.dataset.questionId = question.id;
  field.required = Boolean(question.required);
  label.append(field);
  return label;
}

function artifactButton(path: string): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "artifact";
  button.dataset.artifactPath = path;
  const kind = document.createElement("span");
  kind.textContent = (path.split(".").at(-1) ?? "FILE").toUpperCase();
  const name = document.createElement("strong");
  name.textContent = path.split(/[\\/]/).at(-1) ?? path;
  const hint = document.createElement("small");
  hint.textContent = "在默认程序中打开";
  button.append(kind, name, hint);
  return button;
}

export function renderTaskState(state: TaskState): void {
  const labels: Record<TaskState["status"], string> = {
    idle: "未开始",
    running: "执行中",
    waiting: "等待补充",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  const status = document.querySelector<HTMLElement>("#task-status");
  const progress = document.querySelector<HTMLOListElement>("#progress");
  const questionPanel = document.querySelector<HTMLElement>("#questions-panel");
  const questionForm = document.querySelector<HTMLFormElement>("#questions-form");
  const artifacts = document.querySelector<HTMLElement>("#artifacts");
  const errorBox = document.querySelector<HTMLElement>("#task-error");
  const errorText = document.querySelector<HTMLElement>("#task-error-text");
  if (!status || !progress || !questionPanel || !questionForm || !artifacts || !errorBox || !errorText) return;
  status.textContent = labels[state.status];
  status.dataset.status = state.status;
  const progressItems = state.progress.length ? state.progress : ["生成后会在这里显示进度"];
  progress.replaceChildren(
    ...progressItems.map((item) => {
      const row = document.createElement("li");
      row.textContent = item;
      if (!state.progress.length) row.className = "muted";
      return row;
    }),
  );
  questionPanel.classList.toggle("hidden", state.status !== "waiting");
  questionForm.replaceChildren(...state.questions.map(questionField));
  errorBox.classList.toggle("hidden", !state.error);
  errorText.textContent = state.error;
  artifacts.replaceChildren(...state.artifacts.map(artifactButton));
  renderDebugPanel(state);
}

function renderDebugPanel(state: TaskState): void {
  const details = document.querySelector<HTMLDetailsElement>("#debug-details");
  const count = document.querySelector<HTMLElement>("#debug-count");
  const list = document.querySelector<HTMLOListElement>("#debug-list");
  const pathEl = document.querySelector<HTMLElement>("#debug-log-path");
  if (!details || !count || !list || !pathEl) return;
  const wasOpen = details.open;
  count.textContent = `(${String(state.debug.length)})`;
  if (state.status === "failed" && state.debug.length) details.open = true;
  else if (!wasOpen && state.debug.length) {
    // leave collapsed by default; user can expand
  }
  pathEl.textContent = state.debugLogPath ? `完整日志：${state.debugLogPath}` : "";
  list.replaceChildren(...state.debug.map(formatDebugEntry));
}

function formatDebugEntry(entry: DebugEntry): HTMLLIElement {
  const li = document.createElement("li");
  li.className = `debug-row debug-${entry.phase} debug-${entry.success === false ? "fail" : entry.success === true ? "ok" : "info"}`;
  const head = document.createElement("span");
  head.className = "debug-head";
  const time = entry.ts ? new Date(entry.ts * 1000).toLocaleTimeString() : "";
  const label = DEBUG_LABELS[entry.phase] ?? entry.phase;
  head.textContent = `[${time}] ${label}`;
  const body = document.createElement("span");
  body.className = "debug-body";
  body.append(...describeDebugEntry(entry));
  li.append(head, body);
  return li;
}

const DEBUG_LABELS: Record<string, string> = {
  task_start: "任务启动",
  materials_staged: "材料暂存",
  model_turn: "模型回合",
  tool_call: "工具调用",
  tool_result: "工具结果",
  backend_log: "后端日志",
};

function describeDebugEntry(entry: DebugEntry): ChildNode[] {
  const nodes: ChildNode[] = [];
  const line = (text: string): void => {
    if (text) nodes.push(document.createTextNode(text));
  };
  const br = (): void => {
    nodes.push(document.createElement("br"));
  };
  switch (entry.phase) {
    case "task_start":
      line(`skill=${entry.skill_id ?? "?"} materials=${String(entry.materials_count ?? 0)}`);
      if (entry.user_prompt_preview) {
        br();
        line(`prompt: ${entry.user_prompt_preview}`);
      }
      break;
    case "materials_staged":
      line(entry.staged?.length ? entry.staged.join(", ") : "(无材料)");
      break;
    case "model_turn":
      line(`step ${String(entry.step ?? "?")}，${String(entry.tool_calls?.length ?? 0)} 个工具调用`);
      if (entry.assistant_text_preview) {
        br();
        line(`模型说：${entry.assistant_text_preview}`);
      }
      for (const tc of entry.tool_calls ?? []) {
        br();
        line(`→ ${tc.name}(${tc.args_preview})`);
      }
      break;
    case "tool_call":
      line(`${entry.tool ?? "?"}(${entry.args_preview ?? ""})`);
      break;
    case "tool_result":
      line(`${entry.tool ?? "?"}：${entry.success ? "成功" : "失败"}`);
      if (entry.error) {
        br();
        line(`错误：${entry.error}`);
      } else if (entry.content_preview) {
        br();
        line(`结果：${entry.content_preview}`);
      }
      break;
    case "backend_log":
      line(entry.content_preview ?? "");
      break;
    default:
      line(JSON.stringify(entry));
  }
  return nodes;
}
