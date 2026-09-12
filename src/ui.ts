import type { DebugEntry, Question, TaskState } from "./types";

/* ===== 模块状态 ===== */
let taskMeta: { tool: string; title: string; taskId: string } | null = null;
let taskStartedAt = 0;
let lastTaskState: TaskState | null = null;
let renderedDebug: TaskState["debug"] | null = null;

const STATUS_LABELS: Record<TaskState["status"], string> = {
  idle: "未开始",
  running: "执行中",
  waiting: "等待补充",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_NAMES = ["解析材料", "澄清确认", "生成正文", "版式检查", "写入产物"];

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch] ?? ch);
}

/** 记录当前任务的归属工具与标题，供 renderTaskState 定向渲染 */
export function setTaskMeta(meta: { tool: string; title: string; taskId: string }): void {
  taskMeta = meta;
  taskStartedAt = Date.now();
  lastTaskState = null;
}

/* ===== 导航 ===== */
function goTool(tool: string): void {
  if (!showTool(tool)) return;
  refreshVisibleTaskView();
  const active = document.querySelector<HTMLElement>(`#page-${tool} .subtab.active`);
  notifyArtifactView(tool, active?.dataset.sub);
}

function showTool(tool: string): boolean {
  if (!document.getElementById(`page-${tool}`)) tool = "docx";
  const target = document.getElementById(`page-${tool}`);
  if (target && !target.hidden) return false;
  document.querySelectorAll<HTMLElement>(".page").forEach((p) => {
    p.hidden = p.id !== `page-${tool}`;
  });
  document.querySelectorAll<HTMLElement>(".nav-item[data-tool]").forEach((n) => {
    n.classList.toggle("active", n.dataset.tool === tool);
  });
  return true;
}

export function goSub(tool: string, sub: string): void {
  if (tool === "pdf") { tool = "docx"; sub = "art"; }
  const toolChanged = showTool(tool);
  const page = document.getElementById(`page-${tool}`);
  if (!page) return;
  const target = document.getElementById(`sub-${tool}-${sub}`);
  if (!toolChanged && target && !target.hidden) {
    notifyArtifactView(tool, sub);
    return;
  }
  page.querySelectorAll<HTMLElement>(".subtab").forEach((t) => t.classList.toggle("active", t.dataset.sub === sub));
  // Data-driven: toggle every subpage named after a subtab on this page
  // (new/run/art for tools, providers/services for settings).
  for (const tab of page.querySelectorAll<HTMLElement>(".subtab")) {
    const name = tab.dataset.sub;
    if (!name) continue;
    const el = document.getElementById(`sub-${tool}-${name}`);
    if (el) el.hidden = name !== sub;
  }
  refreshVisibleTaskView();
  notifyArtifactView(tool, sub);
}

function notifyArtifactView(tool: string, sub: string | undefined): void {
  // 控制器负责缓存及合并刷新，导航只通知可见状态。
  if (sub === "art") {
    window.dispatchEvent(new CustomEvent("dockit:artifacts-view-shown", { detail: { tool } }));
  }
}

export function flashSubtab(tool: string, sub: string): void {
  const tab = document.getElementById(`page-${tool}`)?.querySelector<HTMLElement>(`.subtab[data-sub="${sub}"]`);
  if (!tab) return;
  tab.classList.add("flash");
  setTimeout(() => tab.classList.remove("flash"), 1800);
}

function refreshVisibleTaskView(): void {
  if (!lastTaskState) return;
  if (taskMeta) renderRunView(taskMeta.tool, lastTaskState);
  renderDebugPanel(lastTaskState);
}

/* ===== Toast ===== */
export function toast(msg: string, type: "ok" | "info" | "warn" = "ok"): void {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${type === "info" ? "info" : type === "warn" ? "warn" : ""}`;
  el.innerHTML = `<span>${type === "ok" ? "✅" : type === "warn" ? "⚠️" : "ℹ️"}</span><span>${escapeHtml(msg)}</span>`;
  box.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    el.style.transition = "all .3s";
    setTimeout(() => el.remove(), 320);
  }, 2600);
}

/* ===== 模型摘要 chip ===== */
export function setModelSummary(text: string): void {
  const el = document.getElementById("model-summary");
  if (el) el.textContent = text;
}

export function setStartupState(state: "loading" | "ready" | "failed", message?: string): void {
  const ready = state === "ready";
  document.querySelectorAll<HTMLElement>("[data-pick], [data-start], #w-model, #model-picker, #page-settings")
    .forEach((control) => { control.inert = !ready; });
  const status = document.getElementById("startup-status");
  if (!status) return;
  status.hidden = ready;
  status.textContent = ready ? "" : message ?? (state === "failed"
    ? "应用准备失败，请关闭窗口后重试。" : "正在准备本地设置，您可以先浏览页面、填写内容。" );
  status.setAttribute("role", state === "failed" ? "alert" : "status");
}

/* ===== 主题切换 ===== */
const THEME_KEY = "dockit-theme";

function initTheme(): void {
  const dark = localStorage.getItem(THEME_KEY) === "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  updateThemeUi(dark);
}

function toggleTheme(): void {
  const dark = document.documentElement.getAttribute("data-theme") !== "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
  updateThemeUi(dark);
}

function updateThemeUi(dark: boolean): void {
  const btn = document.getElementById("theme-btn");
  if (btn) btn.textContent = dark ? "☀ 浅色模式" : "🌗 深色模式";
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#000000" : "#f2f4f9");
}

/* ===== 页数模式 ===== */
function setPagesMode(m: "exact" | "range"): void {
  document.querySelectorAll<HTMLElement>(".p-mode").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
  const isRange = m === "range";
  const max = document.getElementById("pages-max");
  const sep = document.getElementById("pages-sep");
  if (max) max.hidden = !isRange;
  if (sep) sep.hidden = !isRange;
  if (isRange) document.querySelectorAll<HTMLElement>("#pages-quick .chip").forEach((c) => c.classList.remove("on"));
}

/* ===== 布局 ===== */
export function mountLayout(root: HTMLElement): void {
  lastTaskState = null;
  renderedDebug = null;
  root.innerHTML = `
  <main class="app">
    <aside class="side">
      <div class="brand">
        <div class="brand-icon">✦</div>
        <div><div class="brand-name">DocKit</div><div class="brand-sub">AI 文档产物工具箱</div></div>
      </div>
      <div class="nav-label">V1 工具</div>
      <div class="nav-item active" data-tool="ppt"><span class="nav-icon">📊</span><span>PPT 生成</span></div>
      <div class="nav-item" data-tool="resume"><span class="nav-icon">📄</span><span>简历生成</span></div>
      <div class="nav-item" data-tool="docx"><span class="nav-icon">📝</span><span>DOCX 生成</span></div>
      <div class="nav-label">其他</div>
      <div class="nav-item" data-tool="settings"><span class="nav-icon">⚙</span><span>设置</span></div>
      <div class="nav-item" data-tool="logs"><span class="nav-icon">🪵</span><span>运行日志</span></div>
      <div class="side-card">
        <div class="t">🔒 数据只在本地</div>
        <div class="d">材料与产物保存在你选择的目录中，仅按你的模型配置发出请求。</div>
      </div>
    </aside>

    <section class="main">
      <div class="hello">
        <div>
          <div class="h">下午好 👋 今天要把什么材料变成成品文件？</div>
          <div class="h-sub">从左侧选择一个工具开始 —— 填信息 → 答少量问题 → 拿到文件</div>
        </div>
        <div class="w-model" id="w-model" data-go="settings"><span class="live"></span><span id="model-summary">未配置模型</span><span class="w-chev">▾</span></div>
        <div class="model-picker" id="model-picker" hidden>
          <div class="mp-head">切换供应商 / 模型</div>
          <div class="mp-cols">
            <div class="mp-col">
              <div class="mp-col-title">供应商</div>
              <div class="mp-providers" id="mp-providers"></div>
            </div>
            <div class="mp-col">
              <div class="mp-col-title">模型 <span class="mp-hint" id="mp-models-hint"></span></div>
              <div class="mp-models" id="mp-models"><div class="mp-empty">点击左侧供应商获取模型列表</div></div>
            </div>
          </div>
        </div>
        <button class="theme-btn" id="theme-btn">🌗 深色模式</button>
      </div>
      <div id="startup-status" class="startup-status" role="status" hidden></div>
      <div id="form-error" class="form-error" role="alert"></div>

      <!-- ============ PPT ============ -->
      <section class="page" id="page-ppt">
        <nav class="subtabs">
          <div class="subtab active" data-sub="new">新建任务</div>
          <div class="subtab" data-sub="run">进行中 <span class="badge" hidden>1</span></div>
          <div class="subtab" data-sub="art">产物版本 <span class="badge" hidden>0</span></div>
        </nav>
        <div class="subpage" id="sub-ppt-new">
          <div class="card">
            <div class="card-title"><span class="no">1</span> 演示主题 <span class="sub">必填</span></div>
            <input class="inp" id="ppt-topic" placeholder="例如：Q3 产品复盘汇报">
          </div>
          <div class="card">
            <div class="card-title"><span class="no">2</span> 上传参考材料 <span class="sub">可选 · 用于丰富演示内容</span></div>
            <div class="upload-big" data-pick="ppt" tabindex="0" role="button" aria-label="选择参考材料">
              <div class="u-ic">⭳</div>
              <div class="u-t">拖入文件，或点击选择</div>
              <div class="u-optional">可选 · 不上传也能生成</div>
              <span class="fmt">pdf / docx / md / txt</span>
            </div>
            <input id="materials-input-ppt" type="file" multiple hidden>
            <div id="materials-list-ppt"></div>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">3</span> 风格与页数</div>
            <label class="fld-l" style="margin-top:0;">风格</label>
            <div class="chips" id="ppt-style">
              <span class="chip on" data-v="商务简洁">商务简洁</span>
              <span class="chip" data-v="学术答辩">学术答辩</span>
              <span class="chip" data-v="活泼宣讲">活泼宣讲</span>
              <span class="chip" data-v="极简黑白">极简黑白</span>
            </div>
            <label class="fld-l">目标页数 <span class="sub2">— 精确值或区间均可</span></label>
            <div class="pages-row">
              <div class="chips" id="pages-quick">
                <span class="chip on" data-v="10">约 10 页</span>
                <span class="chip" data-v="20">约 20 页</span>
                <span class="chip" data-v="30">约 30 页</span>
              </div>
              <div class="pages-custom">
                <span class="p-mode on" data-m="exact">精确</span>
                <span class="p-mode" data-m="range">区间</span>
                <span class="p-fields">
                  <input class="inp p-num mono" id="pages-min" value="10" inputmode="numeric">
                  <span class="p-sep" id="pages-sep" hidden>–</span>
                  <input class="inp p-num mono" id="pages-max" value="15" inputmode="numeric" hidden>
                  <span class="p-unit">页</span>
                </span>
              </div>
            </div>
            <div class="cta-row">
              <button class="btn" data-start="ppt">开始生成 PPT</button>
              <span class="btn-note">缺失关键信息时会先收到少量澄清问题</span>
            </div>
          </div>
        </div>
        <div class="subpage" id="sub-ppt-run" hidden>
          <div id="run-ppt"><div class="empty">暂无进行中的任务 —— <a data-go="ppt:new">去新建任务</a></div></div>
        </div>
        <div class="subpage" id="sub-ppt-art" hidden>
          <div id="art-ppt"><div class="empty">暂无产物，生成后的 .pptx 会出现并保留在这里</div></div>
        </div>
      </section>

      <!-- ============ 简历 ============ -->
      <section class="page" id="page-resume" hidden>
        <nav class="subtabs">
          <div class="subtab active" data-sub="new">新建任务</div>
          <div class="subtab" data-sub="run">进行中 <span class="badge" hidden>1</span></div>
          <div class="subtab" data-sub="art">产物版本 <span class="badge" hidden>0</span></div>
        </nav>
        <div class="subpage" id="sub-resume-new">
          <div class="card">
            <div class="card-title"><span class="no">1</span> 目标岗位 <span class="sub">必填</span></div>
            <input class="inp" id="resume-role" placeholder="可留空，开始后确认；例如：深度学习算法工程师（校招）">
            <details class="more"><summary>补充要求（可选）</summary>
              <textarea class="txa" id="resume-extra" placeholder="如：突出实习项目，弱化社团经历…"></textarea>
            </details>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">2</span> 上传经历材料 <span class="sub">可选 · 没有材料也可以开始问答</span></div>
            <div class="upload-big" data-pick="resume" tabindex="0" role="button" aria-label="选择经历材料">
              <div class="u-ic">⭳</div>
              <div class="u-t">拖入文件，或点击选择</div>
              <div class="u-optional">可选 · 不上传也能生成</div>
              <span class="fmt">pdf / docx / md / txt / jpg / png（jpg/png 作证件照）</span>
            </div>
            <input id="materials-input-resume" type="file" multiple hidden>
            <div id="materials-list-resume"></div>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">3</span> 选择模板 <span class="sub">必选</span></div>
            <div class="tmpl-grid" id="resume-template">
              <div class="tmpl-card on" data-template="t001" tabindex="0" role="button" aria-label="选择模板 通用简洁风">
                <img src="resume-templates/t001.jpg" alt="通用简洁风" loading="lazy">
                <div class="tmpl-name">通用简洁风</div>
              </div>
              <div class="tmpl-card" data-template="t002" tabindex="0" role="button" aria-label="选择模板 简约风">
                <img src="resume-templates/t002.jpg" alt="简约风" loading="lazy">
                <div class="tmpl-name">简约风</div>
              </div>
              <div class="tmpl-card" data-template="t046" tabindex="0" role="button" aria-label="选择模板 蓝灰设计风">
                <img src="resume-templates/t046.jpg" alt="蓝灰设计风" loading="lazy">
                <div class="tmpl-name">蓝灰设计风</div>
              </div>
              <div class="tmpl-card" data-template="t026" tabindex="0" role="button" aria-label="选择模板 黑色双栏">
                <img src="resume-templates/t026.jpg" alt="黑色双栏" loading="lazy">
                <div class="tmpl-name">黑色双栏</div>
              </div>
              <div class="tmpl-card" data-template="t109" tabindex="0" role="button" aria-label="选择模板 简约 word">
                <img src="resume-templates/t109.jpg" alt="简约 word" loading="lazy">
                <div class="tmpl-name">简约 word</div>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">4</span> 输出选项</div>
            <label class="fld-l" style="margin-top:0;">格式</label>
            <div class="chips" id="resume-format">
              <span class="chip on" data-v="DOCX">DOCX</span>
              <span class="chip" data-v="PDF">PDF</span>
              <span class="chip" data-v="DOCX+PDF">两者都要</span>
            </div>
            <label class="fld-l">篇幅</label>
            <div class="chips" id="resume-length">
              <span class="chip on" data-v="一页">一页（推荐）</span>
              <span class="chip" data-v="两页">两页</span>
            </div>
            <div class="cta-row">
              <button class="btn" data-start="resume">开始生成简历</button>
              <span class="btn-note">即将上线 · 自动检查超页 / 溢出 / 层级</span>
            </div>
          </div>
        </div>
        <div class="subpage" id="sub-resume-run" hidden>
          <div id="run-resume"><div class="empty">暂无进行中的任务 —— <a data-go="resume:new">去新建任务</a></div></div>
        </div>
        <div class="subpage" id="sub-resume-art" hidden>
          <div id="art-resume"><div class="empty">暂无产物，生成后的文件会出现并保留在这里</div></div>
        </div>
      </section>

      <!-- ============ DOCX ============ -->
      <section class="page" id="page-docx" hidden>
        <nav class="subtabs">
          <div class="subtab active" data-sub="new">新建任务</div>
          <div class="subtab" data-sub="run">进行中 <span class="badge" hidden>1</span></div>
          <div class="subtab" data-sub="art">产物版本 <span class="badge" hidden>0</span></div>
        </nav>
        <div class="subpage" id="sub-docx-new">
          <div class="card">
            <div class="card-title"><span class="no">1</span> 文档用途</div>
            <div class="chips" id="use-chips">
              <span class="chip on" data-v="工作报告">工作报告</span>
              <span class="chip" data-v="会议纪要">会议纪要</span>
              <span class="chip" data-v="总结复盘">总结复盘</span>
              <span class="chip" data-v="项目方案">项目方案</span>
              <span class="chip" data-v="作业报告">作业报告</span>
              <span class="chip" data-v="__other">✎ 其他…</span>
            </div>
            <input class="inp mono" id="use-custom" style="margin-top:10px;display:none;" placeholder="自定义用途，例如：实验报告、读书笔记…">
          </div>
          <div class="card">
            <div class="card-title"><span class="no">2</span> 结构化要求 <span class="sub">主要输入，写越清楚产物越准确</span></div>
            <textarea class="txa" id="docx-req" style="min-height:100px;" placeholder="例如：面向管理层的 Q3 销售复盘：业绩总览 → 区域对比表 → 改进计划，约 8 页"></textarea>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">3</span> 生成复杂度 <span class="sub">对应不同的生成细粒度</span></div>
            <div class="chips" id="docx-complexity">
              <span class="chip" data-v="simple">简单</span>
              <span class="chip on" data-v="standard">标准（封面+目录+页码）</span>
              <span class="chip" data-v="academic">学术（论文版式）</span>
              <span class="chip" data-v="gongwen">公文（GB/T 9704）</span>
              <span class="chip" data-v="form">填报表单</span>
              <span class="chip" data-v="template">模板套用</span>
            </div>
            <div class="vision-warn" id="docx-vision-warn" hidden>
              ⚠️ 当前模型没有视觉能力，将会走无视觉的处理流程，可能会导致一些布局问题，如表格跨页错位、图片溢出或截断、封面与目录分页异常等。
              若您知晓其具有视觉能力，您可以在「设置」的模型页面中将它的视觉标签点亮。
            </div>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">4</span> 上传参考材料 <span class="sub">可选 · 提供素材可显著提升产物贴合度</span></div>
            <div class="upload-big" data-pick="docx" tabindex="0" role="button" aria-label="选择参考材料">
              <div class="u-ic">⭳</div>
              <div class="u-t">拖入文件，或点击选择</div>
              <div class="u-optional">可选 · 不上传也能生成</div>
              <span class="fmt">pdf / docx / md / txt</span>
            </div>
            <input id="materials-input-docx" type="file" multiple hidden>
            <div id="materials-list-docx"></div>
            <div class="cta-row">
              <button class="btn" data-start="docx">开始生成文档</button>
              <span class="btn-note">生成后用 Word / WPS 继续编辑</span>
            </div>
          </div>
        </div>
        <div class="subpage" id="sub-docx-run" hidden>
          <div id="run-docx"><div class="empty">暂无进行中的任务 —— <a data-go="docx:new">去新建任务</a></div></div>
        </div>
        <div class="subpage" id="sub-docx-art" hidden>
          <div id="art-docx"><div class="empty">暂无产物，生成后的 .docx 会出现并保留在这里</div></div>
        </div>
      </section>



      <!-- ============ 设置 ============ -->
      <section class="page" id="page-settings" hidden>
        <nav class="subtabs">
          <div class="subtab active" data-sub="providers">模型供应商</div>
          <div class="subtab" data-sub="services">第三方服务</div>
        </nav>
        <div class="subpage" id="sub-settings-providers">
          <div class="p-cards" id="provider-cards"></div>
          <div class="card">
            <div class="card-title">🔌 当前供应商 <span class="sub" id="provider-card-name-hint">点击上方卡片切换，或添加新的</span></div>
            <div class="set-grid">
              <div>
                <label class="fld-l" style="margin-top:0;">名称</label>
                <input class="inp" id="provider-name" placeholder="例如：OpenAI 主账号">
              </div>
              <div>
                <label class="fld-l" style="margin-top:0;">Provider</label>
                <select class="inp" id="provider-kind">
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                  <option value="openai_compatible" selected>OpenAI-compatible</option>
                </select>
              </div>
            </div>
            <div style="margin-top:12px;">
              <label class="fld-l" style="margin:0;">api_key</label>
              <input class="inp mono" id="api-key" type="password" placeholder="仅在内存中使用" autocomplete="off">
            </div>
            <div style="margin-top:12px;">
              <label class="fld-l" style="margin:0;">base_url <span class="sub2" id="base-url-hint">（兼容 Provider 必填）</span></label>
              <input class="inp mono" id="base-url" placeholder="https://api.your-gateway.com/v1" autocomplete="off">
            </div>
            <div class="model-row">
              <label class="fld-l" style="margin:0;">model</label>
              <div class="model-ctl">
                <select class="inp mono model-sel" id="model">
                  <option value="">— 点击右侧按钮获取模型列表，或手动输入 —</option>
                </select>
                <input class="inp mono model-custom" id="model-custom" style="display:none;" placeholder="手动输入模型名…">
                <button class="btn btn-sm" id="btn-fetch-models">⟳ 获取模型列表</button>
              </div>
              <div class="model-hint" id="model-hint">获取失败时可切换为手动输入</div>
            </div>
            <div class="cta-row">
              <button class="btn" id="btn-save-provider">💾 保存配置</button>
              <button class="btn" id="btn-probe">🔍 检测模型能力</button>
              <span class="caps">
                <span class="cap cap-clickable" id="cap-tool-calling" title="点击可手动关闭/开启该模型的工具调用能力">tool_calling <b>✓</b></span>
                <span class="cap cap-clickable cap-vision" id="cap-vision" title="点击可手动点亮/熄灭该模型的视觉能力标签">vision <b>✓</b></span>
              </span>
              <span class="btn-sec danger" id="btn-delete-provider">🗑 删除此供应商</span>
            </div>
            <div class="probe-grid">
              <div>
                <label class="fld-l" style="margin:0;">生成质量</label>
                <select class="inp" id="reasoning-level">
                  <option value="auto">自动（跟随模型默认）</option>
                  <option value="fast">快速（浅推理）</option>
                  <option value="balanced">平衡（推荐）</option>
                  <option value="deep">深入（重推理）</option>
                </select>
              </div>
              <div id="probe-status" class="probe-status">未检测</div>
            </div>
            <div class="sec-note">输入或切换卡片时已自动持久化到本地数据库；点「保存配置」可立即落盘确认。任务开始前检查模型能力，不兼容时明确报错，不静默降级。tool_calling 默认开启、vision 按模型静态表自动判定，标签可点击手动覆盖。生成质量档位仅在模型支持时生效，不支持时自动省略对应参数。「检测模型能力」会向当前 Provider 发送少量测试请求，可能产生极少量 token 费用。</div>
          </div>
          <div class="card">
            <div class="card-title">📂 输出目录</div>
            <div class="path-row">
              <input class="inp mono" id="output-dir" readonly placeholder="请选择目录">
              <button class="btn btn-sm" id="choose-dir">选择</button>
            </div>
            <div class="sec-note">材料与产物仅保存在本地；工具执行限制在任务目录内，可审计、有超时。</div>
          </div>
        </div>
        <div class="subpage" id="sub-settings-services" hidden>
          <div class="card">
            <div class="card-title">🔗 MinerU 文档解析</div>
            <div class="set-grid">
              <div>
                <label class="fld-l" style="margin-top:0;">API Token</label>
                <input class="inp mono" id="mineru-key" type="password" placeholder="粘贴 MinerU API Token" autocomplete="off">
              </div>
            </div>
            <div class="sec-note">MinerU 是本产品三个功能（PPT / 简历 / DOCX）的<b>强依赖</b>：
              任务开始前会校验 CLI 与 Token，不可用时任务直接失败并给出恢复指引，不会静默降级。
              用于 PDF 输入材料的文档解析（扫描件 OCR / 公式识别）。请在
              <a href="https://mineru.net/apiManage/token" target="_blank" rel="noopener">https://mineru.net/apiManage/token</a>
              登录并创建 Token，复制到此处。
            </div>
            <div id="mineru-status" class="probe-status">MinerU 状态检查中…</div>
          </div>
        </div>
      </section>

      <!-- ============ 运行日志 ============ -->
      <section class="page" id="page-logs" hidden>
        <div class="card">
          <div class="card-title">🪵 运行日志 <span class="badge" id="debug-count">0</span></div>
          <p id="debug-log-path" class="debug-path muted"></p>
          <ol id="debug-list" class="debug-list"></ol>
        </div>
      </section>

      <div class="foot">数据只在本地 · 产物用 Word / WPS / PowerPoint 打开 · 每次反馈生成新版本</div>
    </section>
  </main>

  <!-- 澄清弹窗 -->
  <div id="clarify-modal" class="overlay" hidden>
    <div class="dialog">
      <div class="d-head">💬 需要补充信息 <span class="rounds">模型澄清</span></div>
      <div class="d-sub">缺少的信息会明显影响产物质量，回答后立即继续生成。</div>
      <div id="clarify-questions"></div>
      <div class="d-foot">
        <span class="d-skip" id="clarify-skip">跳过，按当前资料生成 →</span>
        <button class="d-ok" id="submit-answers">提交并继续</button>
      </div>
    </div>
  </div>

  <!-- Toast 容器 -->
  <div id="toasts"></div>`;

  /* ---- 交互接线 ---- */
  initTheme();
  document.getElementById("theme-btn")?.addEventListener("click", toggleTheme);

  root.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    const nav = t.closest<HTMLElement>(".nav-item[data-tool]");
    if (nav) {
      goTool(nav.dataset.tool!);
      // 进入「设置」页时默认落到模型供应商配置（无默认字标签页时的兜底）
      if (nav.dataset.tool === "settings") goSub("settings", "providers");
      return;
    }
    const sub = t.closest<HTMLElement>(".subtab[data-sub]");
    if (sub) {
      const page = sub.closest<HTMLElement>(".page");
      if (page) goSub(page.id.slice(5), sub.dataset.sub!);
      return;
    }
    const go = t.closest<HTMLElement>("[data-go]");
    if (go && go.dataset.go) {
      const [tool, s] = go.dataset.go.split(":");
      goSub(tool, s);
      return;
    }
    const mode = t.closest<HTMLElement>(".p-mode");
    if (mode && mode.dataset.m) {
      setPagesMode(mode.dataset.m as "exact" | "range");
      return;
    }
    const tmpl = t.closest<HTMLElement>(".tmpl-card");
    if (tmpl && tmpl.dataset.template) {
      const grid = tmpl.closest<HTMLElement>(".tmpl-grid");
      grid?.querySelectorAll(".tmpl-card").forEach((c) => c.classList.toggle("on", c === tmpl));
      return;
    }
    const chip = t.closest<HTMLElement>(".chip");
    if (!chip) return;
    const group = chip.closest<HTMLElement>(".chips");
    if (!group) return;
    if (group.hasAttribute("data-multi")) {
      chip.classList.toggle("on");
      return;
    }
    group.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === chip));
    if (group.id === "use-chips") {
      const isOther = chip.dataset.v === "__other";
      const custom = document.getElementById("use-custom");
      if (custom) {
        custom.style.display = isOther ? "block" : "none";
        if (isOther) custom.focus();
      }
    }
    if (group.id === "pages-quick") {
      const v = chip.dataset.v;
      const min = document.getElementById("pages-min") as HTMLInputElement | null;
      if (min && v) min.value = v;
      setPagesMode("exact");
    }
  });

  ["pages-min", "pages-max"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", () => {
      document.querySelectorAll<HTMLElement>("#pages-quick .chip").forEach((c) => c.classList.remove("on"));
    });
  });
}

/* ===== 任务状态渲染 ===== */
export function renderTaskState(state: TaskState): void {
  const previous = lastTaskState;
  lastTaskState = state;
  if (!previous || previous.status !== state.status || previous.artifacts !== state.artifacts) {
    updateBadges(state);
  }
  if (taskMeta && (!previous || previous.status !== state.status || previous.progress !== state.progress
      || previous.qa !== state.qa || previous.materialProgress !== state.materialProgress
      || previous.error !== state.error || previous.visionWarning !== state.visionWarning)) {
    renderRunView(taskMeta.tool, state);
  }
  // 新任务暂时没有产物，不能覆盖目录扫描已显示的历史成果。
  if (taskMeta && state.artifacts.length && (!previous || previous.artifacts !== state.artifacts)) {
    renderArtView(taskMeta.tool, state);
  }
  if (!previous || previous.status !== state.status || previous.questions !== state.questions) {
    renderClarify(state);
  }
  renderDebugPanel(state);
}

function updateBadges(state: TaskState): void {
  document.querySelectorAll<HTMLElement>(".page").forEach((page) => {
    const tool = page.id.slice(5);
    const isTaskTool = taskMeta?.tool === tool;
    const runBadge = page.querySelector<HTMLElement>('.subtab[data-sub="run"] .badge');
    if (runBadge) {
      const active = isTaskTool && (state.status === "running" || state.status === "waiting");
      runBadge.hidden = !active;
      runBadge.textContent = "1";
    }
    const artBadge = page.querySelector<HTMLElement>('.subtab[data-sub="art"] .badge');
    if (artBadge) {
      const count = isTaskTool && state.status === "completed" ? state.artifacts.length : 0;
      artBadge.hidden = count === 0;
      artBadge.textContent = String(count);
    }
  });
}

/** 由状态与进度文本粗粒度推导当前步骤（纯展示） */
function deriveStep(state: TaskState): number {
  if (state.status === "completed") return 5;
  if (state.status === "waiting") return 2;
  const joined = state.progress.join("\n");
  if (/版式|检查|渲染|校验|validate|svg|pdf/i.test(joined)) return 4;
  if (/执行|工具|超时|正在生成|生成正文/i.test(joined)) return 3;
  return 1;
}

function renderRunView(tool: string, state: TaskState): void {
  const host = document.getElementById(`run-${tool}`);
  if (!host || host.closest("[hidden]")) return;
  if (state.status === "idle") {
    host.innerHTML = `<div class="empty">暂无进行中的任务 —— <a data-go="${tool}:new">去新建任务</a></div>`;
    return;
  }
  const step = deriveStep(state);
  const done = step >= 5 ? 5 : Math.max(0, step - 1);
  const width = step >= 5 ? 100 : Math.max(12, Math.round((done / 5) * 100));
  const statusLabel = STATUS_LABELS[state.status];
  const rowtagCls = state.status === "failed" ? "fail" : state.status === "completed" || state.status === "cancelled" ? "done" : "";
  const title = taskMeta?.title ?? "当前任务";
  const tid = taskMeta?.taskId ? taskMeta.taskId.slice(0, 14) : "";
  const elapsed = taskStartedAt ? Math.max(1, Math.round((Date.now() - taskStartedAt) / 1000)) : 0;
  const last = state.progress.at(-1) ?? "";
  const cancellable = state.status === "running" || state.status === "waiting";
  const stepsHtml = STEP_NAMES.map((name, i) => {
    const idx = i + 1;
    const cls = idx <= done ? "done" : idx === step && state.status !== "completed" ? "now" : "";
    const label = cls === "done" ? "✓" : String(idx);
    return `<div class="step ${cls}"><div class="b">${label}</div><div class="sn">${name}</div></div>`;
  }).join("");
  host.innerHTML = `
    <div class="card">
      <div class="card-title">🚀 当前任务</div>
      <div class="taskline"><span>📄</span><span class="t-title">${escapeHtml(title)}</span><span class="tid">${tid}</span><span class="rowtag ${rowtagCls}"><span class="pulse-dot"></span> ${statusLabel}</span></div>
      ${renderVisionWarning(state)}
      ${renderMaterialProgress(state)}
      <div class="steps" style="margin-top:12px;">${stepsHtml}</div>
      <div class="bar"><i class="anim" style="width:${width}%"></i></div>
      <div class="bar-meta"><span class="cur">${escapeHtml(last) || "正在解析输入材料…"}</span><span class="mono">${elapsed ? `~${elapsed}s` : ""}</span></div>
      <div class="cta-row"><button class="btn-sec" data-action="cancel" ${cancellable ? "" : "disabled"}>✕ 取消任务</button></div>
    </div>
    ${renderQAStatus(state)}
    <div id="task-error" class="task-error ${state.error ? "" : "hidden"}">
      <code>${escapeHtml(state.error)}</code>
      <button class="btn-sec" data-action="copy-error">复制错误</button>
    </div>`;
}

/** 无 Vision 提示（§12.3）：开始按钮附近已展示，任务中再补一条状态栏提醒 */
function renderVisionWarning(state: TaskState): string {
  if (state.visionWarning) {
    return `<div class="vision-warn">⚠️ ${escapeHtml(state.visionWarning)}</div>`;
  }
  return "";
}

/** 材料预处理进度（§12.1 事件：parsing/done/failed + 稳定错误码） */
function renderMaterialProgress(state: TaskState): string {
  if (!state.materialProgress.length) return "";
  const rows = state.materialProgress
    .map((p) => {
      const icon = p.phase === "done" ? "✅" : p.phase === "failed" ? "❌" : "⏳";
      const detail = p.phase === "failed" && p.error ? `（${escapeHtml(p.error)}）` : "";
      return `<div class="mprogress">${icon} <span class="mono">${escapeHtml(p.path)}</span> ${escapeHtml(p.phase)}${detail}</div>`;
    })
    .join("");
  return `<div class="materials-progress" style="margin-top:10px;">${rows}</div>`;
}

/** 双 QA 状态（§12.3）：机械检查与视觉检查分开呈现，不能合并 */
function renderQAStatus(state: TaskState): string {
  const qa = state.qa;
  if (!qa.mechanical && qa.visual === "not_run") return "";
  const mech = qa.mechanical === "passed" ? "✅ 通过" : qa.mechanical === "failed" ? "❌ 失败" : "未执行";
  const vis =
    qa.visual === "passed" ? "✅ 通过" : qa.visual === "failed" ? "❌ 失败" : "未执行";
  return `<div class="qa-row card" style="margin-top:10px;">
    <div class="card-title">🧪 质量检查</div>
    <div class="qa-item">结构与机械检查：${mech}${qa.mechanical_issues.length ? `（${qa.mechanical_issues.length} 项）` : ""}</div>
    <div class="qa-item">视觉版式检查：${vis}${qa.visual === "not_run" ? " — 未执行视觉验证" : ""}</div>
    ${qa.used_assets || qa.skipped_assets ? `<div class="qa-item">已采用资源 ${qa.used_assets} 个 · 低置信度舍弃 ${qa.skipped_assets} 个</div>` : ""}
  </div>`;
}

function renderArtView(tool: string, state: TaskState): void {
  const host = document.getElementById(`art-${tool}`);
  if (!host) return;
  if (!state.artifacts.length) {
    host.innerHTML = `<div class="empty">暂无产物，生成后的文件会出现并保留在这里</div>`;
    return;
  }
  host.innerHTML = state.artifacts
    .map((p, i) => {
      const ext = (p.split(".").at(-1) ?? "FILE").toUpperCase();
      const name = p.split(/[\\/]/).at(-1) ?? p;
      const badgeCls = ext === "PDF" ? "v-pdf" : "v-docx";
      const badge = ext === "PDF" ? "P" : "W";
      return `<div class="vrow ${i === 0 ? "cur" : ""}">
        <div class="v-badge ${badgeCls}">${badge}</div>
        <div><div class="v-title">${escapeHtml(name)}</div><div class="v-sub">${escapeHtml(p)} · 可在默认专业软件中打开</div></div>
        <div class="v-open" data-artifact-path="${escapeHtml(p)}">打开</div>
      </div>`;
    })
    .join("");
}

function renderClarify(state: TaskState): void {
  const modal = document.getElementById("clarify-modal");
  const box = document.getElementById("clarify-questions");
  if (!modal || !box) return;
  const show = state.status === "waiting" && state.questions.length > 0;
  modal.hidden = !show;
  if (show) box.replaceChildren(...state.questions.map(questionField));
}

function questionField(question: Question): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "d-item";
  const label = document.createElement("div");
  label.className = "d-q";
  label.append(document.createTextNode(`❓ ${question.label}${question.required ? "（必填）" : ""}`));
  const fieldBox = document.createElement("div");
  fieldBox.className = "d-field";
  let field: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
  if (question.type === "textarea") {
    field = document.createElement("textarea");
    field.className = "txa";
    fieldBox.append(field);
  } else if (question.type === "select") {
    // 下拉选项之外，始终提供一个「✎ 自定义回答…」入口：
    // 选中后展开自由输入框，其内容将作为该问题的回答。
    field = document.createElement("select");
    field.className = "inp mono";
    field.append(
      ...(question.options ?? []).map((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        return option;
      }),
      (() => {
        const custom = document.createElement("option");
        custom.value = "__custom__";
        custom.textContent = "✎ 自定义回答…";
        return custom;
      })(),
    );
    const customBox = document.createElement("input");
    customBox.className = "inp d-custom";
    customBox.placeholder = "输入自定义回答…";
    customBox.dataset.customFor = question.id;
    customBox.hidden = true;
    field.addEventListener("change", () => {
      const isCustom = field.value === "__custom__";
      customBox.hidden = !isCustom;
      if (isCustom) customBox.focus();
    });
    fieldBox.append(field, customBox);
  } else {
    field = document.createElement("input");
    field.className = "inp";
    field.placeholder = "可直接输入自定义回答…";
    fieldBox.append(field);
  }
  field.dataset.questionId = question.id;
  field.required = Boolean(question.required);
  wrap.append(label, fieldBox);
  return wrap;
}

/* ===== 调试面板（一级页：运行日志） ===== */
function renderDebugPanel(state: TaskState): void {
  const count = document.getElementById("debug-count");
  const list = document.getElementById("debug-list");
  const pathEl = document.getElementById("debug-log-path");
  if (!count || !list || !pathEl) return;
  if (list.closest("[hidden]")) return;
  count.textContent = String(state.debug.length);
  pathEl.textContent = state.debugLogPath ? `完整日志：${state.debugLogPath}` : "";
  if (renderedDebug === state.debug) return;
  const previousCount = renderedDebug?.length ?? 0;
  // reducer 只追加日志或在新任务开始时重置，不重建已经显示的行。
  const appendOnly = previousCount > 0 && state.debug.length >= previousCount
    && state.debug[0] === renderedDebug?.[0]
    && state.debug[previousCount - 1] === renderedDebug?.[previousCount - 1];
  if (!appendOnly) list.replaceChildren();
  if (!state.debug.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "暂无日志，任务运行后自动记录";
    list.replaceChildren(empty);
    renderedDebug = state.debug;
    return;
  }
  list.append(...state.debug.slice(appendOnly ? previousCount : 0).map(formatDebugEntry));
  renderedDebug = state.debug;
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
