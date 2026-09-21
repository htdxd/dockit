// src/ui.ts
var taskMeta = null;
var taskStartedAt = 0;
var lastTaskState = null;
var renderedDebug = null;
var STATUS_LABELS = {
  idle: "\u672A\u5F00\u59CB",
  running: "\u6267\u884C\u4E2D",
  waiting: "\u7B49\u5F85\u8865\u5145",
  completed: "\u5DF2\u5B8C\u6210",
  failed: "\u5931\u8D25",
  cancelled: "\u5DF2\u53D6\u6D88"
};
var STEP_NAMES = ["\u89E3\u6790\u6750\u6599", "\u6F84\u6E05\u786E\u8BA4", "\u751F\u6210\u6B63\u6587", "\u7248\u5F0F\u68C0\u67E5", "\u5199\u5165\u4EA7\u7269"];
function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch] ?? ch);
}
function setTaskMeta(meta) {
  taskMeta = meta;
  taskStartedAt = meta.startedAt ?? Date.now();
  lastTaskState = null;
}
function goTool(tool) {
  if (!showTool(tool)) return;
  refreshVisibleTaskView();
  const active2 = document.querySelector(`#page-${tool} .subtab.active`);
  notifyArtifactView(tool, active2?.dataset.sub);
}
function showTool(tool) {
  if (!document.getElementById(`page-${tool}`)) tool = "resume";
  const target = document.getElementById(`page-${tool}`);
  if (target && !target.hidden) return false;
  document.querySelectorAll(".page").forEach((p) => {
    p.hidden = p.id !== `page-${tool}`;
  });
  document.querySelectorAll(".nav-item[data-tool]").forEach((n) => {
    n.classList.toggle("active", n.dataset.tool === tool);
  });
  return true;
}
function goSub(tool, sub) {
  if (tool === "pdf") {
    tool = "resume";
    sub = "art";
  }
  const toolChanged = showTool(tool);
  const page = document.getElementById(`page-${tool}`);
  if (!page) return;
  const target = document.getElementById(`sub-${tool}-${sub}`);
  if (!toolChanged && target && !target.hidden) {
    notifyArtifactView(tool, sub);
    return;
  }
  page.querySelectorAll(".subtab").forEach((t) => t.classList.toggle("active", t.dataset.sub === sub));
  for (const tab of page.querySelectorAll(".subtab")) {
    const name = tab.dataset.sub;
    if (!name) continue;
    const el2 = document.getElementById(`sub-${tool}-${name}`);
    if (el2) el2.hidden = name !== sub;
  }
  refreshVisibleTaskView();
  notifyArtifactView(tool, sub);
}
function notifyArtifactView(tool, sub) {
  if (sub === "art") {
    window.dispatchEvent(new CustomEvent("dockit:artifacts-view-shown", { detail: { tool } }));
  }
}
function refreshVisibleTaskView() {
  if (!lastTaskState) return;
  if (taskMeta) renderRunView(taskMeta.tool, lastTaskState);
  renderDebugPanel(lastTaskState);
}
function toast(msg, type = "ok") {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el2 = document.createElement("div");
  el2.className = `toast ${type === "info" ? "info" : type === "warn" ? "warn" : ""}`;
  el2.innerHTML = `<span>${type === "ok" ? "\u2705" : type === "warn" ? "\u26A0\uFE0F" : "\u2139\uFE0F"}</span><span>${escapeHtml(msg)}</span>`;
  box.appendChild(el2);
  setTimeout(() => {
    el2.style.opacity = "0";
    el2.style.transform = "translateX(20px)";
    el2.style.transition = "all .3s";
    setTimeout(() => el2.remove(), 320);
  }, 2600);
}
var THEME_KEY = "dockit-theme";
function initTheme() {
  const dark = localStorage.getItem(THEME_KEY) === "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  updateThemeUi(dark);
}
function toggleTheme() {
  const dark = document.documentElement.getAttribute("data-theme") !== "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
  updateThemeUi(dark);
}
function updateThemeUi(dark) {
  const btn = document.getElementById("theme-btn");
  if (btn) btn.textContent = dark ? "\u2600 \u6D45\u8272\u6A21\u5F0F" : "\u{1F317} \u6DF1\u8272\u6A21\u5F0F";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#000000" : "#f2f4f9");
}
function setPagesMode(m) {
  document.querySelectorAll(".p-mode").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
  const isRange = m === "range";
  const max = document.getElementById("pages-max");
  const sep = document.getElementById("pages-sep");
  if (max) max.hidden = !isRange;
  if (sep) sep.hidden = !isRange;
  if (isRange) document.querySelectorAll("#pages-quick .chip").forEach((c) => c.classList.remove("on"));
}
function mountLayout(root2, options = {}) {
  lastTaskState = null;
  renderedDebug = null;
  root2.innerHTML = `
  <main class="app">
    <aside class="side">
      <div class="brand">
        <div class="brand-icon">\u2726</div>
        <div><div class="brand-name">DocKit Resume</div><div class="brand-sub">AI \u7B80\u5386\u5236\u4F5C</div></div>
      </div>
      <div class="nav-label">\u7B80\u5386</div>
      <div class="nav-item active" data-tool="resume"><span class="nav-icon">\u{1F4C4}</span><span>\u7B80\u5386\u751F\u6210</span></div>
      <div class="nav-label">\u5176\u4ED6</div>
      <div class="nav-item" data-tool="settings"><span class="nav-icon">\u2699</span><span>\u8BBE\u7F6E</span></div>
      <div class="nav-item" data-tool="logs"><span class="nav-icon">\u{1FAB5}</span><span>\u8FD0\u884C\u65E5\u5FD7</span></div>
    </aside>

    <section class="main">
      <div class="hello">
        <div>
          <div class="h">\u7B80\u5386\u5236\u4F5C</div>
          <div class="h-sub">\u6574\u7406\u7ECF\u5386\u3001\u8FC1\u79FB\u6A21\u677F\uFF0C\u6216\u4FEE\u6539\u5DF2\u6709\u7B80\u5386</div>
        </div>
        <div class="w-model" id="w-model"><span class="live"></span><span id="model-summary">\u672A\u914D\u7F6E\u6A21\u578B</span><span class="w-chev">\u25BE</span></div>
        <div class="model-picker" id="model-picker" hidden>
          <div class="mp-head">\u5207\u6362\u4F9B\u5E94\u5546 / \u6A21\u578B</div>
          <div class="mp-cols">
            <div class="mp-col">
              <div class="mp-col-title">\u4F9B\u5E94\u5546</div>
              <div class="mp-providers" id="mp-providers"></div>
            </div>
            <div class="mp-col">
              <div class="mp-col-title">\u6A21\u578B <span class="mp-hint" id="mp-models-hint"></span></div>
              <div class="mp-models" id="mp-models"><div class="mp-empty">\u70B9\u51FB\u5DE6\u4FA7\u4F9B\u5E94\u5546\u83B7\u53D6\u6A21\u578B\u5217\u8868</div></div>
            </div>
          </div>
        </div>
        <button class="theme-btn" id="theme-btn">\u{1F317} \u6DF1\u8272\u6A21\u5F0F</button>
      </div>
      <div id="startup-status" class="startup-status" role="status" hidden></div>
      <div id="form-error" class="form-error" role="alert"></div>

      <!-- ============ \u7B80\u5386 ============ -->
      <section class="page" id="page-resume">
        <nav class="subtabs">
          <div class="subtab active" data-sub="new">\u65B0\u5EFA\u4EFB\u52A1</div>
          <div class="subtab" data-sub="run">\u8FDB\u884C\u4E2D <span class="badge" hidden>1</span></div>
          <div class="subtab" data-sub="art">\u4EA7\u7269\u7248\u672C <span class="badge" hidden>0</span></div>
        </nav>
        <div class="subpage" id="sub-resume-new">
          <div class="card">
            <div class="card-title"><span class="no">1</span> \u76EE\u6807\u5C97\u4F4D <span class="sub">\u5FC5\u586B</span></div>
            <input class="inp" id="resume-role" placeholder="\u53EF\u7559\u7A7A\uFF0C\u5F00\u59CB\u540E\u786E\u8BA4\uFF1B\u4F8B\u5982\uFF1A\u6DF1\u5EA6\u5B66\u4E60\u7B97\u6CD5\u5DE5\u7A0B\u5E08\uFF08\u6821\u62DB\uFF09">
            <details class="more"><summary>\u8865\u5145\u8981\u6C42\uFF08\u53EF\u9009\uFF09</summary>
              <textarea class="txa" id="resume-extra" placeholder="\u5982\uFF1A\u7A81\u51FA\u5B9E\u4E60\u9879\u76EE\uFF0C\u5F31\u5316\u793E\u56E2\u7ECF\u5386\u2026"></textarea>
            </details>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">2</span> \u4E0A\u4F20\u7ECF\u5386\u6750\u6599 <span class="sub">\u53EF\u9009 \xB7 \u6CA1\u6709\u6750\u6599\u4E5F\u53EF\u4EE5\u5F00\u59CB\u95EE\u7B54</span></div>
            <div class="upload-big" data-pick="resume" tabindex="0" role="button" aria-label="\u9009\u62E9\u7ECF\u5386\u6750\u6599">
              <div class="u-ic">\u2B73</div>
              <div class="u-t">\u62D6\u5165\u6587\u4EF6\uFF0C\u6216\u70B9\u51FB\u9009\u62E9</div>
              <div class="u-optional">\u53EF\u9009 \xB7 \u4E0D\u4E0A\u4F20\u4E5F\u80FD\u751F\u6210</div>
              <span class="fmt">pdf / docx / md / txt / jpg / png\uFF08\u652F\u6301\u7B80\u5386\u622A\u56FE\u4E0E\u8BC1\u4EF6\u7167\uFF0C\u53EF\u591A\u6B21\u6DFB\u52A0\uFF09</span>
            </div>
            <input id="materials-input-resume" type="file" multiple hidden>
            <div class="manual-entry"><button type="button" class="btn btn-sm" id="resume-manual">\u270E \u624B\u52A8\u586B\u5199</button><span id="manual-status">\u53EF\u4E0E\u4E0A\u4F20\u6750\u6599\u4E00\u8D77\u4F7F\u7528</span></div>
            <div id="materials-list-resume"></div>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">3</span> \u9009\u62E9\u6A21\u677F <span class="sub">\u5FC5\u9009</span></div>
            <div class="tmpl-grid" id="resume-template">
              <div class="tmpl-card on" data-template="t001" tabindex="0" role="button" aria-label="\u9009\u62E9\u6A21\u677F \u901A\u7528\u7B80\u6D01\u98CE">
                <img src="resume-templates/t001.jpg" alt="\u901A\u7528\u7B80\u6D01\u98CE" loading="lazy">
                <div class="tmpl-name">\u901A\u7528\u7B80\u6D01\u98CE</div>
              </div>
              <div class="tmpl-card" data-template="t109" tabindex="0" role="button" aria-label="\u9009\u62E9\u6A21\u677F \u7B80\u7EA6 word">
                <img src="resume-templates/t109.jpg" alt="\u7B80\u7EA6 word" loading="lazy">
                <div class="tmpl-name">\u7B80\u7EA6 word</div>
              </div>
              <div class="tmpl-card" data-template="t002" tabindex="0" role="button" aria-label="\u9009\u62E9\u6A21\u677F \u7B80\u7EA6\u7EBF\u6761">
                <img src="resume-templates/t002.jpg" alt="\u7B80\u7EA6\u7EBF\u6761" loading="lazy">
                <div class="tmpl-name">\u7B80\u7EA6\u7EBF\u6761</div>
              </div>
              <div class="tmpl-card" data-template="t003" tabindex="0" role="button" aria-label="\u9009\u62E9\u6A21\u677F \u5E94\u5C4A\u6BD5\u4E1A\u751F">
                <img src="resume-templates/t003.jpg" alt="\u5E94\u5C4A\u6BD5\u4E1A\u751F" loading="lazy">
                <div class="tmpl-name">\u5E94\u5C4A\u6BD5\u4E1A\u751F</div>
              </div>
              <div class="tmpl-card" data-template="t015" tabindex="0" role="button" aria-label="\u9009\u62E9\u6A21\u677F \u884C\u653F\u7BA1\u7406">
                <img src="resume-templates/t015.jpg" alt="\u884C\u653F\u7BA1\u7406" loading="lazy">
                <div class="tmpl-name">\u884C\u653F\u7BA1\u7406</div>
              </div>
              <div class="tmpl-card" data-template="t024" tabindex="0" role="button" aria-label="\u9009\u62E9\u6A21\u677F \u53CC\u680F\u6E05\u6670">
                <img src="resume-templates/t024.jpg" alt="\u53CC\u680F\u6E05\u6670" loading="lazy">
                <div class="tmpl-name">\u53CC\u680F\u6E05\u6670</div>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-title"><span class="no">4</span> \u8F93\u51FA\u9009\u9879</div>
            <label class="fld-l" style="margin-top:0;">\u683C\u5F0F</label>
            <div class="chips" id="resume-format">
              <span class="chip on" data-v="DOCX">DOCX</span>
              <span class="chip" data-v="PDF">PDF</span>
              <span class="chip" data-v="DOCX+PDF">\u4E24\u8005\u90FD\u8981</span>
            </div>
            <label class="fld-l">\u5185\u5BB9\u4F18\u5316\u7A0B\u5EA6</label>
            <div class="chips" id="resume-writing-style">
              <span class="chip" data-v="light">\u8F7B\u5EA6\u6DA6\u8272</span>
              <span class="chip on" data-v="balanced">\u7A81\u51FA\u4F18\u52BF</span>
              <span class="chip" data-v="strong">\u6DF1\u5EA6\u6539\u5199</span>
            </div>
            <div class="sub2">\u8F7B\u5EA6\u6DA6\u8272\uFF1A\u6574\u7406\u63AA\u8F9E\uFF1B\u7A81\u51FA\u4F18\u52BF\uFF1A\u7CBE\u9009\u8D21\u732E\u4E0E\u6210\u679C\uFF1B\u6DF1\u5EA6\u6539\u5199\uFF1A\u91CD\u7EC4\u5185\u5BB9\u548C\u8868\u8FBE\u3002\u5747\u4FDD\u7559\u4E8B\u5B9E\uFF0C\u7F3A\u5C11\u7ECF\u5386\u7EC6\u8282\u65F6\u4F1A\u5148\u8BE2\u95EE\u3002</div>
            <label class="fld-l">\u7BC7\u5E45</label>
            <div class="chips" id="resume-length">
              <span class="chip on" data-v="\u4E00\u9875">\u4E00\u9875\uFF08\u63A8\u8350\uFF09</span>
              <span class="chip" data-v="\u4E24\u9875">\u4E24\u9875</span>
            </div>
            <div class="cta-row">
              <button class="btn" data-start="resume">\u5F00\u59CB\u751F\u6210\u7B80\u5386</button>
            </div>
          </div>
        </div>
        <div class="subpage" id="sub-resume-run" hidden>
          <div id="run-resume"><div class="empty">\u6682\u65E0\u8FDB\u884C\u4E2D\u7684\u4EFB\u52A1 \u2014\u2014 <a data-go="resume:new">\u53BB\u65B0\u5EFA\u4EFB\u52A1</a></div></div>
        </div>
        <div class="subpage" id="sub-resume-art" hidden>
          <div id="art-resume"><div class="empty">\u6682\u65E0\u4EA7\u7269\uFF0C\u751F\u6210\u540E\u7684\u6587\u4EF6\u4F1A\u51FA\u73B0\u5E76\u4FDD\u7559\u5728\u8FD9\u91CC</div></div>
        </div>
      </section>

      <!-- ============ \u8BBE\u7F6E ============ -->
      <section class="page" id="page-settings" hidden>
        <nav class="subtabs">
          <div class="subtab active" data-sub="providers">\u6A21\u578B\u4F9B\u5E94\u5546</div>
          <div class="subtab" data-sub="services">\u7B2C\u4E09\u65B9\u670D\u52A1</div>
        </nav>
        <div class="subpage" id="sub-settings-providers">
          <div class="p-cards" id="provider-cards"></div>
          <div class="card">
            <div class="card-title">\u{1F50C} \u5F53\u524D\u4F9B\u5E94\u5546 <span class="sub" id="provider-card-name-hint">\u70B9\u51FB\u4E0A\u65B9\u5361\u7247\u5207\u6362\uFF0C\u6216\u6DFB\u52A0\u65B0\u7684</span></div>
            <div class="set-grid">
              <div>
                <label class="fld-l" style="margin-top:0;">\u540D\u79F0</label>
                <input class="inp" id="provider-name" placeholder="\u4F8B\u5982\uFF1AOpenAI \u4E3B\u8D26\u53F7">
              </div>
              <div>
                <label class="fld-l" style="margin-top:0;">Provider</label>
                <select class="inp" id="provider-kind">
                  <option value="openai">OpenAI Chat Completions</option>
                  <option value="openai_responses">OpenAI Responses</option>
                  <option value="anthropic">Anthropic</option>
                  <option value="openai_compatible" selected>OpenAI-compatible \xB7 Chat Completions</option>
                </select>
              </div>
            </div>
            <div style="margin-top:12px;">
              <label class="fld-l" style="margin:0;">api_key</label>
              <input class="inp mono" id="api-key" type="password" placeholder="\u4EC5\u5728\u5185\u5B58\u4E2D\u4F7F\u7528" autocomplete="off">
            </div>
            <div style="margin-top:12px;">
              <label class="fld-l" style="margin:0;">base_url <span class="sub2" id="base-url-hint">\uFF08\u517C\u5BB9 Provider \u5FC5\u586B\uFF09</span></label>
              <input class="inp mono" id="base-url" placeholder="https://api.your-gateway.com/v1" autocomplete="off">
            </div>
            <div class="model-row">
              <label class="fld-l" style="margin:0;">model</label>
              <div class="model-ctl">
                <div class="settings-model-picker" id="settings-model-picker">
                  <input class="inp mono" id="model" placeholder="\u8F93\u5165\u6A21\u578B\u540D\uFF0C\u6216\u70B9\u51FB\u53F3\u4FA7\u7BAD\u5934\u9009\u62E9" autocomplete="off" role="combobox" aria-label="\u6A21\u578B\u540D" aria-controls="model-options" aria-expanded="false" aria-autocomplete="none">
                  <button type="button" id="model-toggle" class="model-toggle" aria-label="\u5C55\u5F00\u6A21\u578B\u5217\u8868" aria-haspopup="listbox" aria-expanded="false" aria-controls="model-options">\u25BE</button>
                  <div id="model-options" class="model-options" role="listbox" aria-label="\u6A21\u578B\u5217\u8868" hidden></div>
                </div>
                <button class="btn btn-sm" id="btn-fetch-models">\u27F3 \u83B7\u53D6\u6A21\u578B\u5217\u8868</button>
              </div>
              <div class="model-hint" id="model-hint">\u6846\u5185\u53EF\u76F4\u63A5\u8F93\u5165\uFF1B\u53F3\u4FA7\u7BAD\u5934\u5C55\u5F00\u5DF2\u83B7\u53D6\u7684\u6A21\u578B\u5217\u8868</div>
            </div>
            <div class="cta-row">
              <button class="btn" id="btn-save-provider">\u{1F4BE} \u4FDD\u5B58\u914D\u7F6E</button>
              <button class="btn" id="btn-probe">\u{1F50D} \u68C0\u6D4B\u6A21\u578B\u80FD\u529B</button>
              <span class="caps">
                <span class="cap cap-clickable" id="cap-tool-calling" title="\u70B9\u51FB\u53EF\u624B\u52A8\u5173\u95ED/\u5F00\u542F\u8BE5\u6A21\u578B\u7684\u5DE5\u5177\u8C03\u7528\u80FD\u529B">tool_calling <b>\u2713</b></span>
                <span class="cap cap-clickable cap-vision" id="cap-vision" title="\u70B9\u51FB\u53EF\u624B\u52A8\u70B9\u4EAE/\u7184\u706D\u8BE5\u6A21\u578B\u7684\u89C6\u89C9\u80FD\u529B\u6807\u7B7E">vision <b>\u2713</b></span>
              </span>
              <span class="btn-sec danger" id="btn-delete-provider">\u{1F5D1} \u5220\u9664\u6B64\u4F9B\u5E94\u5546</span>
            </div>
            <div class="probe-grid">
              <div>
                <label class="fld-l" style="margin:0;">\u63A8\u7406\u6DF1\u5EA6</label>
                <select class="inp" id="reasoning-level">
                  <option value="auto">\u81EA\u52A8\uFF08\u8DDF\u968F\u6A21\u578B\u9ED8\u8BA4\uFF09</option>
                  <option value="none">\u5173\u95ED \xB7 none</option>
                  <option value="minimal">\u6700\u5C11 \xB7 minimal</option>
                  <option value="low">\u4F4E \xB7 low</option>
                  <option value="medium">\u4E2D \xB7 medium</option>
                  <option value="high">\u9AD8 \xB7 high</option>
                  <option value="xhigh">\u8D85\u9AD8 \xB7 xhigh</option>
                  <option value="max">\u6700\u9AD8 \xB7 max</option>
                </select>
              </div>
              <div id="probe-status" class="probe-status">\u672A\u68C0\u6D4B</div>
            </div>
            <div class="sec-note">\u914D\u7F6E\u81EA\u52A8\u4FDD\u5B58\u3002\u80FD\u529B\u6807\u7B7E\u8DDF\u968F\u68C0\u6D4B\u7ED3\u679C\uFF0C\u4E5F\u53EF\u70B9\u51FB\u624B\u52A8\u8986\u76D6\u3002\u5F3A\u5236\u5DE5\u5177\u8C03\u7528\u4EC5\u5728\u5F53\u524D\u63A8\u7406\u6DF1\u5EA6\u4E0B\u68C0\u6D4B\u901A\u8FC7\u540E\u542F\u7528\uFF0C\u5426\u5219\u81EA\u52A8\u9009\u62E9\u5DE5\u5177\u3002\u63A8\u7406\u6DF1\u5EA6\u6309\u534F\u8BAE\u63D0\u4F9B\u9009\u9879\uFF0C\u5177\u4F53\u53EF\u7528\u6863\u4F4D\u53D6\u51B3\u4E8E\u6A21\u578B\uFF1B\u81EA\u52A8\u4E0D\u53D1\u9001\u63A8\u7406\u53C2\u6570\u3002Anthropic \u5F53\u524D\u4EE5\u601D\u8003\u9884\u7B97\u63A7\u5236\u4F4E/\u4E2D/\u9AD8\u3002\u300C\u68C0\u6D4B\u6A21\u578B\u80FD\u529B\u300D\u4F1A\u53D1\u9001\u5C11\u91CF\u6D4B\u8BD5\u8BF7\u6C42\uFF0C\u53EF\u80FD\u4EA7\u751F\u5C11\u91CF\u8D39\u7528\u3002</div>
          </div>
          <div class="card">
            <div class="card-title">\u{1F4C2} \u8F93\u51FA\u76EE\u5F55</div>
            <div class="path-row">
              <input class="inp mono" id="output-dir" readonly placeholder="\u8BF7\u9009\u62E9\u76EE\u5F55">
              <button class="btn btn-sm" id="choose-dir">\u9009\u62E9</button>
            </div>
            <div class="sec-note">\u684C\u9762\u7248\u6750\u6599\u4E0E\u4EA7\u7269\u4FDD\u5B58\u5728\u672C\u5730\uFF1B\u751F\u6210\u65F6\u76F8\u5173\u5185\u5BB9\u4F1A\u53D1\u9001\u5230\u6240\u9009\u6A21\u578B\u4E0E MinerU \u670D\u52A1\u3002</div>
          </div>
        </div>
        <div class="subpage" id="sub-settings-services" hidden>
          <div class="card">
            <div class="card-title">\u{1F517} MinerU \u6587\u6863\u89E3\u6790</div>
            <div class="set-grid">
              <div>
                <label class="fld-l" style="margin-top:0;">API Token</label>
                <input class="inp mono" id="mineru-key" type="password" placeholder="\u7C98\u8D34 MinerU API Token" autocomplete="off">
              </div>
            </div>
            <div class="sec-note">MinerU \u7528\u4E8E PDF/DOCX \u6750\u6599\u89E3\u6790\uFF1A
              \u4EFB\u52A1\u5F00\u59CB\u524D\u4F1A\u6821\u9A8C CLI \u4E0E Token\uFF0C\u4E0D\u53EF\u7528\u65F6\u4EFB\u52A1\u76F4\u63A5\u5931\u8D25\u5E76\u7ED9\u51FA\u6062\u590D\u6307\u5F15\uFF0C\u4E0D\u4F1A\u9759\u9ED8\u964D\u7EA7\u3002
              \u7528\u4E8E PDF \u8F93\u5165\u6750\u6599\u7684\u6587\u6863\u89E3\u6790\uFF08\u626B\u63CF\u4EF6 OCR / \u516C\u5F0F\u8BC6\u522B\uFF09\u3002\u8BF7\u5728
              <a href="https://mineru.net/apiManage/token" target="_blank" rel="noopener">https://mineru.net/apiManage/token</a>
              \u767B\u5F55\u5E76\u521B\u5EFA Token\uFF0C\u590D\u5236\u5230\u6B64\u5904\u3002
            </div>
            <div id="mineru-status" class="probe-status">MinerU \u72B6\u6001\u68C0\u67E5\u4E2D\u2026</div>
          </div>
        </div>
      </section>

      <!-- ============ \u8FD0\u884C\u65E5\u5FD7 ============ -->
      <section class="page" id="page-logs" hidden>
        <div class="card">
          <div class="card-title">\u{1FAB5} \u8FD0\u884C\u65E5\u5FD7 <span class="badge" id="debug-count">0</span></div>
          <p id="debug-log-path" class="debug-path muted"></p>
          <ol id="debug-list" class="debug-list"></ol>
        </div>
      </section>

      <div class="foot">\u751F\u6210\u4E0E\u89E3\u6790\u4F1A\u5411\u914D\u7F6E\u7684 AI / MinerU \u670D\u52A1\u53D1\u9001\u76F8\u5173\u6750\u6599\u3002<a href="https://github.com/htdxd/dockit" target="_blank" rel="noopener">\u6E90\u7801 \xB7 AGPL-3.0</a></div>
    </section>
  </main>

  <!-- \u6F84\u6E05\u5F39\u7A97 -->
  <div id="clarify-modal" class="overlay" hidden>
    <div class="dialog">
      <div class="d-head">\u{1F4AC} \u9700\u8981\u8865\u5145\u4FE1\u606F <span class="rounds">\u6A21\u578B\u6F84\u6E05</span></div>
      <div class="d-sub">\u7F3A\u5C11\u7684\u4FE1\u606F\u4F1A\u660E\u663E\u5F71\u54CD\u4EA7\u7269\u8D28\u91CF\uFF0C\u56DE\u7B54\u540E\u7ACB\u5373\u7EE7\u7EED\u751F\u6210\u3002</div>
      <div id="clarify-questions"></div>
      <div class="d-foot">
        <span class="d-skip" id="clarify-skip">\u8DF3\u8FC7\uFF0C\u6309\u5F53\u524D\u8D44\u6599\u751F\u6210 \u2192</span>
        <button class="d-ok" id="submit-answers">\u63D0\u4EA4\u5E76\u7EE7\u7EED</button>
      </div>
    </div>
  </div>

  <div id="manual-resume-modal" class="overlay" hidden>
    <div class="dialog manual-dialog" role="dialog" aria-modal="true" aria-labelledby="manual-title">
      <div class="d-head" id="manual-title">\u270E \u586B\u5199\u7B80\u5386\u8D44\u6599</div>
      <div class="d-sub">\u6309\u5B9E\u9645\u60C5\u51B5\u586B\u5199\uFF0C\u6682\u65F6\u6CA1\u6709\u7684\u4FE1\u606F\u53EF\u4EE5\u7559\u7A7A\uFF1B\u4FDD\u5B58\u540E\u53EF\u7EE7\u7EED\u4E0A\u4F20\u6587\u4EF6\uFF0C\u4E00\u8D77\u751F\u6210\u7B80\u5386\u3002</div>
      <div class="manual-fields">
        <label>\u57FA\u672C\u4FE1\u606F<textarea id="manual-basic" placeholder="\u59D3\u540D\u3001\u8054\u7CFB\u65B9\u5F0F\u3001\u6C42\u804C\u610F\u5411\uFF1B\u5176\u4ED6\u5E0C\u671B\u5C55\u793A\u7684\u4FE1\u606F\u4E5F\u53EF\u586B\u5199\u3002"></textarea></label>
        <label>\u6559\u80B2\u80CC\u666F<textarea id="manual-education" placeholder="\u5B66\u6821\u3001\u4E13\u4E1A\u3001\u5B66\u5386\u3001\u8D77\u6B62\u65F6\u95F4\uFF1B\u76F8\u5173\u8BFE\u7A0B\u3001\u6210\u7EE9\u6216\u8363\u8A89\uFF08\u9009\u586B\uFF09\u3002"></textarea></label>
        <label class="manual-experience">\u9879\u76EE\u3001\u5DE5\u4F5C\u53CA\u5176\u4ED6\u7ECF\u5386<textarea id="manual-experience" placeholder="\u5DE5\u4F5C\u3001\u5B9E\u4E60\u3001\u9879\u76EE\u3001\u793E\u56E2\u3001\u7ADE\u8D5B\u7B49\u90FD\u5199\u5728\u8FD9\u91CC\uFF0C\u6BCF\u6BB5\u7ECF\u5386\u5206\u5F00\u5199\u3002\u5EFA\u8BAE\u8BF4\u660E\uFF1A\u540D\u79F0\u4E0E\u65F6\u95F4\u3001\u4F60\u7684\u89D2\u8272\u3001\u505A\u4E86\u4EC0\u4E48\u3001\u4F7F\u7528\u7684\u65B9\u6CD5\u6216\u6280\u672F\u3001\u5B9E\u9645\u6210\u679C\u3002\u6CA1\u6709\u51C6\u786E\u6570\u636E\u53EF\u63CF\u8FF0\u771F\u5B9E\u4EA7\u51FA\uFF0C\u4E0D\u5FC5\u7F16\u9020\u3002"></textarea></label>
        <label>\u6280\u80FD\u3001\u6210\u679C\u4E0E\u8865\u5145\u8BF4\u660E<textarea id="manual-skills" placeholder="\u6280\u80FD\u3001\u8BC1\u4E66\u3001\u5956\u9879\uFF1B\u53EF\u63D0\u4F9B GitHub\uFF0F\u4F5C\u54C1\u96C6\u94FE\u63A5\u3001\u9879\u76EE Stars/Forks \u6216\u4E0B\u8F7D\u91CF\uFF08\u7528\u6237\u63D0\u4F9B\u5373\u53EF\uFF0C\u4E0D\u5FC5\u9F50\u5168\uFF09\uFF0C\u4EE5\u53CA\u5E0C\u671B\u52A0\u7C97\u3001\u4E3B\u9898\u8272\u5F3A\u8C03\u6216\u4E0D\u5C55\u793A\u7684\u4FE1\u606F\u3002"></textarea></label>
      </div>
      <div class="d-foot"><button type="button" class="btn" id="manual-cancel">\u53D6\u6D88</button><button type="button" class="d-ok" id="manual-save">\u4FDD\u5B58\u8D44\u6599</button></div>
    </div>
  </div>
  <!-- Toast \u5BB9\u5668 -->
  <div id="toasts"></div>`;
  if (options.resumeOnly) {
    root2.querySelectorAll('.page:not(#page-resume), .nav-item:not([data-tool="resume"]), #w-model, #model-picker, #startup-status').forEach((element) => element.remove());
    root2.querySelectorAll(".nav-label").forEach((element) => element.remove());
    root2.querySelector('.nav-item[data-tool="resume"]')?.classList.add("active");
    root2.querySelector("#page-resume").hidden = false;
  }
  initTheme();
  document.getElementById("theme-btn")?.addEventListener("click", toggleTheme);
  root2.addEventListener("click", (e) => {
    const t = e.target;
    const nav = t.closest(".nav-item[data-tool]");
    if (nav) {
      goTool(nav.dataset.tool);
      if (nav.dataset.tool === "settings") goSub("settings", "providers");
      return;
    }
    const sub = t.closest(".subtab[data-sub]");
    if (sub) {
      const page = sub.closest(".page");
      if (page) goSub(page.id.slice(5), sub.dataset.sub);
      return;
    }
    const go = t.closest("[data-go]");
    if (go && go.dataset.go) {
      const [tool, s] = go.dataset.go.split(":");
      goSub(tool, s);
      return;
    }
    const mode = t.closest(".p-mode");
    if (mode && mode.dataset.m) {
      setPagesMode(mode.dataset.m);
      return;
    }
    const tmpl = t.closest(".tmpl-card");
    if (tmpl && tmpl.dataset.template) {
      const grid = tmpl.closest(".tmpl-grid");
      grid?.querySelectorAll(".tmpl-card").forEach((c) => c.classList.toggle("on", c === tmpl));
      return;
    }
    const chip = t.closest(".chip");
    if (!chip) return;
    const group = chip.closest(".chips");
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
      const min = document.getElementById("pages-min");
      if (min && v) min.value = v;
      setPagesMode("exact");
    }
  });
  ["pages-min", "pages-max"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", () => {
      document.querySelectorAll("#pages-quick .chip").forEach((c) => c.classList.remove("on"));
    });
  });
}
function renderTaskState(state) {
  const previous = lastTaskState;
  lastTaskState = state;
  if (!previous || previous.status !== state.status || previous.artifacts !== state.artifacts) {
    updateBadges(state);
  }
  if (taskMeta && (!previous || previous.status !== state.status || previous.progress !== state.progress || previous.qa !== state.qa || previous.materialProgress !== state.materialProgress || previous.error !== state.error || previous.visionWarning !== state.visionWarning)) {
    renderRunView(taskMeta.tool, state);
  }
  if (taskMeta && state.artifacts.length && (!previous || previous.artifacts !== state.artifacts)) {
    renderArtView(taskMeta.tool, state);
  }
  if (!previous || previous.status !== state.status || previous.questions !== state.questions) {
    renderClarify(state);
  }
  renderDebugPanel(state);
}
function updateBadges(state) {
  document.querySelectorAll(".page").forEach((page) => {
    const tool = page.id.slice(5);
    const isTaskTool = taskMeta?.tool === tool;
    const runBadge = page.querySelector('.subtab[data-sub="run"] .badge');
    if (runBadge) {
      const active2 = isTaskTool && (state.status === "running" || state.status === "waiting");
      runBadge.hidden = !active2;
      runBadge.textContent = "1";
    }
    const artBadge = page.querySelector('.subtab[data-sub="art"] .badge');
    if (artBadge) {
      const count = isTaskTool && state.status === "completed" ? state.artifacts.length : 0;
      artBadge.hidden = count === 0;
      artBadge.textContent = String(count);
    }
  });
}
function deriveStep(state) {
  if (state.status === "completed") return 5;
  if (state.status === "waiting") return 2;
  const joined = state.progress.join("\n");
  if (/版式|检查|渲染|校验|validate|svg|pdf/i.test(joined)) return 4;
  if (/执行|工具|超时|正在生成|生成正文/i.test(joined)) return 3;
  return 1;
}
function renderRunView(tool, state) {
  const host = document.getElementById(`run-${tool}`);
  if (!host || host.closest("[hidden]")) return;
  if (state.status === "idle") {
    host.innerHTML = `<div class="empty">\u6682\u65E0\u8FDB\u884C\u4E2D\u7684\u4EFB\u52A1 \u2014\u2014 <a data-go="${tool}:new">\u53BB\u65B0\u5EFA\u4EFB\u52A1</a></div>`;
    return;
  }
  const step = deriveStep(state);
  const done = step >= 5 ? 5 : Math.max(0, step - 1);
  const width = step >= 5 ? 100 : Math.max(12, Math.round(done / 5 * 100));
  const statusLabel = STATUS_LABELS[state.status];
  const rowtagCls = state.status === "failed" ? "fail" : state.status === "completed" || state.status === "cancelled" ? "done" : "";
  const title = taskMeta?.title ?? "\u5F53\u524D\u4EFB\u52A1";
  const tid = taskMeta?.taskId ? taskMeta.taskId.slice(0, 14) : "";
  const elapsed = taskStartedAt ? Math.max(1, Math.round((Date.now() - taskStartedAt) / 1e3)) : 0;
  const last = state.progress.at(-1) ?? "";
  const cancellable = state.status === "running" || state.status === "waiting";
  const stepsHtml = STEP_NAMES.map((name, i) => {
    const idx = i + 1;
    const cls = idx <= done ? "done" : idx === step && state.status !== "completed" ? "now" : "";
    const label = cls === "done" ? "\u2713" : String(idx);
    return `<div class="step ${cls}"><div class="b">${label}</div><div class="sn">${name}</div></div>`;
  }).join("");
  host.innerHTML = `
    <div class="card">
      <div class="card-title">\u{1F680} \u5F53\u524D\u4EFB\u52A1</div>
      <div class="taskline"><span>\u{1F4C4}</span><span class="t-title">${escapeHtml(title)}</span><span class="tid">${tid}</span><span class="rowtag ${rowtagCls}"><span class="pulse-dot"></span> ${statusLabel}</span></div>
      ${renderVisionWarning(state)}
      ${renderMaterialProgress(state)}
      <div class="steps" style="margin-top:12px;">${stepsHtml}</div>
      <div class="bar"><i class="anim" style="width:${width}%"></i></div>
      <div class="bar-meta"><span class="cur">${escapeHtml(last) || "\u6B63\u5728\u89E3\u6790\u8F93\u5165\u6750\u6599\u2026"}</span><span class="mono">${elapsed ? `~${elapsed}s` : ""}</span></div>
      <div class="cta-row"><button class="btn-sec" data-action="cancel" ${cancellable ? "" : "disabled"}>\u2715 \u53D6\u6D88\u4EFB\u52A1</button></div>
    </div>
    ${renderQAStatus(state)}
    <div id="task-error" class="task-error ${state.error ? "" : "hidden"}">
      <code>${escapeHtml(state.error)}</code>
      <button class="btn-sec" data-action="copy-error">\u590D\u5236\u9519\u8BEF</button>
    </div>`;
}
function renderVisionWarning(state) {
  if (state.visionWarning) {
    return `<div class="vision-warn">\u26A0\uFE0F ${escapeHtml(state.visionWarning)}</div>`;
  }
  return "";
}
function renderMaterialProgress(state) {
  if (!state.materialProgress.length) return "";
  const rows = state.materialProgress.map((p) => {
    const icon = p.phase === "done" ? "\u2705" : p.phase === "failed" ? "\u274C" : "\u23F3";
    const detail = p.phase === "failed" && p.error ? `\uFF08${escapeHtml(p.error)}\uFF09` : "";
    return `<div class="mprogress">${icon} <span class="mono">${escapeHtml(p.path)}</span> ${escapeHtml(p.phase)}${detail}</div>`;
  }).join("");
  return `<div class="materials-progress" style="margin-top:10px;">${rows}</div>`;
}
function renderQAStatus(state) {
  const qa = state.qa;
  if (!qa.mechanical && qa.visual === "not_run") return "";
  const mech = qa.mechanical === "passed" ? "\u2705 \u901A\u8FC7" : qa.mechanical === "failed" ? "\u274C \u5931\u8D25" : "\u672A\u6267\u884C";
  const vis = qa.visual === "passed" ? "\u2705 \u901A\u8FC7" : qa.visual === "failed" ? "\u274C \u5931\u8D25" : "\u672A\u6267\u884C";
  return `<div class="qa-row card" style="margin-top:10px;">
    <div class="card-title">\u{1F9EA} \u8D28\u91CF\u68C0\u67E5</div>
    <div class="qa-item">\u7ED3\u6784\u4E0E\u673A\u68B0\u68C0\u67E5\uFF1A${mech}${qa.mechanical_issues.length ? `\uFF08${qa.mechanical_issues.length} \u9879\uFF09` : ""}</div>
    <div class="qa-item">\u89C6\u89C9\u7248\u5F0F\u68C0\u67E5\uFF1A${vis}${qa.visual === "not_run" ? " \u2014 \u672A\u6267\u884C\u89C6\u89C9\u9A8C\u8BC1" : ""}</div>
    ${qa.used_assets || qa.skipped_assets ? `<div class="qa-item">\u5DF2\u91C7\u7528\u8D44\u6E90 ${qa.used_assets} \u4E2A \xB7 \u4F4E\u7F6E\u4FE1\u5EA6\u820D\u5F03 ${qa.skipped_assets} \u4E2A</div>` : ""}
  </div>`;
}
function renderArtView(tool, state) {
  const host = document.getElementById(`art-${tool}`);
  if (!host) return;
  if (!state.artifacts.length) {
    host.innerHTML = `<div class="empty">\u6682\u65E0\u4EA7\u7269\uFF0C\u751F\u6210\u540E\u7684\u6587\u4EF6\u4F1A\u51FA\u73B0\u5E76\u4FDD\u7559\u5728\u8FD9\u91CC</div>`;
    return;
  }
  host.innerHTML = state.artifacts.map((p, i) => {
    const ext = (p.split(".").at(-1) ?? "FILE").toUpperCase();
    const name = p.split(/[\\/]/).at(-1) ?? p;
    const badgeCls = ext === "PDF" ? "v-pdf" : "v-docx";
    const badge = ext === "PDF" ? "P" : "W";
    return `<div class="vrow ${i === 0 ? "cur" : ""}">
        <div class="v-badge ${badgeCls}">${badge}</div>
        <div><div class="v-title">${escapeHtml(name)}</div><div class="v-sub">${escapeHtml(p)} \xB7 \u53EF\u5728\u9ED8\u8BA4\u4E13\u4E1A\u8F6F\u4EF6\u4E2D\u6253\u5F00</div></div>
        <div class="v-open" data-artifact-path="${escapeHtml(p)}">\u6253\u5F00</div>
      </div>`;
  }).join("");
}
function renderClarify(state) {
  const modal = document.getElementById("clarify-modal");
  const box = document.getElementById("clarify-questions");
  if (!modal || !box) return;
  const show = state.status === "waiting" && state.questions.length > 0;
  modal.hidden = !show;
  if (show) box.replaceChildren(...state.questions.map(questionField));
}
function questionField(question) {
  const wrap = document.createElement("div");
  wrap.className = "d-item";
  const label = document.createElement("div");
  label.className = "d-q";
  label.append(document.createTextNode(`\u2753 ${question.label}${question.required ? "\uFF08\u5FC5\u586B\uFF09" : ""}`));
  const fieldBox = document.createElement("div");
  fieldBox.className = "d-field";
  let field;
  if (question.type === "textarea") {
    field = document.createElement("textarea");
    field.className = "txa";
    fieldBox.append(field);
  } else if (question.type === "select") {
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
        custom.textContent = "\u270E \u81EA\u5B9A\u4E49\u56DE\u7B54\u2026";
        return custom;
      })()
    );
    const customBox = document.createElement("input");
    customBox.className = "inp d-custom";
    customBox.placeholder = "\u8F93\u5165\u81EA\u5B9A\u4E49\u56DE\u7B54\u2026";
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
    field.placeholder = "\u53EF\u76F4\u63A5\u8F93\u5165\u81EA\u5B9A\u4E49\u56DE\u7B54\u2026";
    fieldBox.append(field);
  }
  field.dataset.questionId = question.id;
  field.required = Boolean(question.required);
  wrap.append(label, fieldBox);
  return wrap;
}
function renderDebugPanel(state) {
  const count = document.getElementById("debug-count");
  const list = document.getElementById("debug-list");
  const pathEl = document.getElementById("debug-log-path");
  if (!count || !list || !pathEl) return;
  if (list.closest("[hidden]")) return;
  count.textContent = String(state.debug.length);
  pathEl.textContent = state.debugLogPath ? `\u5B8C\u6574\u65E5\u5FD7\uFF1A${state.debugLogPath}` : "";
  if (renderedDebug === state.debug) return;
  const previousCount = renderedDebug?.length ?? 0;
  const appendOnly = previousCount > 0 && state.debug.length >= previousCount && state.debug[0] === renderedDebug?.[0] && state.debug[previousCount - 1] === renderedDebug?.[previousCount - 1];
  if (!appendOnly) list.replaceChildren();
  if (!state.debug.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "\u6682\u65E0\u65E5\u5FD7\uFF0C\u4EFB\u52A1\u8FD0\u884C\u540E\u81EA\u52A8\u8BB0\u5F55";
    list.replaceChildren(empty);
    renderedDebug = state.debug;
    return;
  }
  list.append(...state.debug.slice(appendOnly ? previousCount : 0).map(formatDebugEntry));
  renderedDebug = state.debug;
}
function formatDebugEntry(entry) {
  const li = document.createElement("li");
  li.className = `debug-row debug-${entry.phase} debug-${entry.success === false ? "fail" : entry.success === true ? "ok" : "info"}`;
  const head = document.createElement("span");
  head.className = "debug-head";
  const time = entry.ts ? new Date(entry.ts * 1e3).toLocaleTimeString() : "";
  const label = DEBUG_LABELS[entry.phase] ?? entry.phase;
  head.textContent = `[${time}] ${label}`;
  const body = document.createElement("span");
  body.className = "debug-body";
  body.append(...describeDebugEntry(entry));
  li.append(head, body);
  return li;
}
var DEBUG_LABELS = {
  task_start: "\u4EFB\u52A1\u542F\u52A8",
  materials_staged: "\u6750\u6599\u6682\u5B58",
  model_turn: "\u6A21\u578B\u56DE\u5408",
  tool_call: "\u5DE5\u5177\u8C03\u7528",
  tool_result: "\u5DE5\u5177\u7ED3\u679C",
  backend_log: "\u540E\u7AEF\u65E5\u5FD7"
};
function describeDebugEntry(entry) {
  const nodes = [];
  const line = (text) => {
    if (text) nodes.push(document.createTextNode(text));
  };
  const br = () => {
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
      line(entry.staged?.length ? entry.staged.join(", ") : "(\u65E0\u6750\u6599)");
      break;
    case "model_turn":
      line(`step ${String(entry.step ?? "?")}\uFF0C${String(entry.tool_calls?.length ?? 0)} \u4E2A\u5DE5\u5177\u8C03\u7528`);
      if (entry.assistant_text_preview) {
        br();
        line(`\u6A21\u578B\u8BF4\uFF1A${entry.assistant_text_preview}`);
      }
      for (const tc of entry.tool_calls ?? []) {
        br();
        line(`\u2192 ${tc.name}(${tc.args_preview})`);
      }
      break;
    case "tool_call":
      line(`${entry.tool ?? "?"}(${entry.args_preview ?? ""})`);
      break;
    case "tool_result":
      line(`${entry.tool ?? "?"}\uFF1A${entry.success ? "\u6210\u529F" : "\u5931\u8D25"}`);
      if (entry.error) {
        br();
        line(`\u9519\u8BEF\uFF1A${entry.error}`);
      } else if (entry.content_preview) {
        br();
        line(`\u7ED3\u679C\uFF1A${entry.content_preview}`);
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

// src/state.ts
var initialTaskState = {
  status: "idle",
  progress: [],
  questions: [],
  artifacts: [],
  error: "",
  debug: [],
  debugLogPath: "",
  materialProgress: [],
  qa: emptyQA(),
  visionWarning: ""
};
function emptyQA() {
  return {
    mechanical: "",
    mechanical_issues: [],
    visual: "not_run",
    visual_issues: [],
    repair_rounds: 0,
    used_assets: 0,
    skipped_assets: 0
  };
}

// src/web.ts
var root = document.getElementById("app");
mountLayout(root, { resumeOnly: true });
var el = (id) => document.getElementById(id);
var input = (id) => el(id);
var active = (id) => el(id).querySelector(".on")?.dataset.v ?? "";
var files = [];
var jobs = [];
var current = localStorage.getItem("dockit_task") ?? "";
var loggedIn = false;
var lastSnapshot = "";
var metaId = "";
var questionKey = "";
var cachedQuestions = [];
var manual = {};
var manualNames = { basic: "\u57FA\u672C\u4FE1\u606F", education: "\u6559\u80B2\u80CC\u666F", experience: "\u9879\u76EE\u3001\u5DE5\u4F5C\u53CA\u5176\u4ED6\u7ECF\u5386", skills: "\u6280\u80FD\u3001\u6210\u679C\u4E0E\u8865\u5145\u8BF4\u660E" };
var labels = { queued: "\u6392\u961F\u4E2D", running: "\u751F\u6210\u4E2D", waiting: "\u5F85\u8865\u5145", completed: "\u5DF2\u5B8C\u6210", failed: "\u5931\u8D25", cancelled: "\u5DF2\u53D6\u6D88" };
root.querySelector(".brand-sub").textContent = "AI \u7B80\u5386\u751F\u6210";
root.querySelector(".h-sub").textContent = "\u586B\u4FE1\u606F \u2192 \u7B54\u5C11\u91CF\u95EE\u9898 \u2192 \u62FF\u5230\u6587\u4EF6";
root.querySelector(".foot").innerHTML = '\u6750\u6599\u4E0E\u4EA7\u7269\u4FDD\u5B58\u5728\u670D\u52A1\u5668\uFF1B\u89E3\u6790\u4E0E\u751F\u6210\u4F1A\u53D1\u9001\u81F3\u914D\u7F6E\u7684 AI / MinerU \u670D\u52A1\u3002 <a href="https://github.com/htdxd/dockit" target="_blank" rel="noopener">\u6E90\u7801 \xB7 AGPL-3.0</a>';
root.querySelector("#page-resume .fmt").textContent = "pdf / docx / md / txt / jpg / png / webp \xB7 \u6700\u591A 6 \u4E2A\u6587\u4EF6\uFF0C\u603B\u8BA1 30 MB";
root.querySelector(".hello").insertAdjacentHTML("beforeend", '<span id="web-quota"></span><button class="btn-sec" id="web-logout" hidden>\u9000\u51FA</button>');
el("sub-resume-new").insertAdjacentHTML("beforeend", '<label class="web-consent"><input id="web-consent" type="checkbox"> \u6211\u540C\u610F\u5C06\u6750\u6599\u53D1\u9001\u81F3\u7F51\u7AD9\u914D\u7F6E\u7684 AI / MinerU \u670D\u52A1\u3002\u6750\u6599\u548C\u4EFB\u52A1\u65E5\u5FD7\u4FDD\u5B58 <span id="web-retention">7</span> \u5929\u3002</label>');
el("sub-resume-run").insertAdjacentHTML("afterbegin", '<select class="inp web-history" id="web-history" aria-label="\u9009\u62E9\u4EFB\u52A1"></select>');
root.insertAdjacentHTML("beforeend", '<dialog id="web-login" class="dialog"><form id="web-login-form"><div class="d-head">DocKit Resume</div><p class="d-sub">\u8F93\u5165\u4E2A\u4EBA\u9080\u8BF7\u7801\uFF0C\u67E5\u770B\u4F60\u7684\u4EFB\u52A1\u548C\u6210\u54C1\u3002\u8BF7\u52FF\u5171\u4EAB\u9080\u8BF7\u7801\u3002</p><input class="inp" id="web-code" aria-label="\u9080\u8BF7\u7801" autocomplete="off" required><p id="web-login-error"></p><div class="d-foot"><button class="d-ok">\u8FDB\u5165</button></div></form></dialog>');
var loginDialog = el("web-login");
loginDialog.oncancel = (event) => event.preventDefault();
async function api(path, options = {}) {
  const response = await fetch("/api" + path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      loggedIn = false;
      if (!loginDialog.open) loginDialog.showModal();
    }
    throw new Error(typeof data.detail === "string" ? data.detail : "\u8BF7\u6C42\u5931\u8D25\uFF0C\u8BF7\u91CD\u8BD5");
  }
  return data;
}
var post = (path, data = {}) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
var run = (fn) => () => void fn().catch((error) => toast(error.message, "warn"));
function renderFiles() {
  el("materials-list-resume").innerHTML = files.map((file, index) => `<div class="frow">\u{1F4CE} ${escapeHtml(file.name)} <span class="fsz">${Math.round(file.size / 1024)} KB</span><button type="button" class="fx" data-remove-file="${index}">\xD7</button></div>`).join("");
}
function addFiles(selected) {
  for (const file of selected) if (!files.some((f) => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified)) files.push(file);
  renderFiles();
}
var fileInput = input("materials-input-resume");
fileInput.accept = ".pdf,.docx,.txt,.md,.png,.jpg,.jpeg,.webp";
fileInput.onchange = () => {
  addFiles(Array.from(fileInput.files ?? []));
  fileInput.value = "";
};
var upload = root.querySelector('[data-pick="resume"]');
upload.onclick = () => fileInput.click();
upload.onkeydown = (e) => {
  if (["Enter", " "].includes(e.key)) {
    e.preventDefault();
    fileInput.click();
  }
};
upload.ondragover = (e) => e.preventDefault();
upload.ondrop = (e) => {
  e.preventDefault();
  addFiles(Array.from(e.dataTransfer?.files ?? []));
};
root.addEventListener("click", (e) => {
  const target = e.target.closest("[data-remove-file]");
  if (target) {
    files.splice(Number(target.dataset.removeFile), 1);
    renderFiles();
  }
});
el("resume-manual").onclick = () => {
  for (const key of Object.keys(manualNames)) input("manual-" + key).value = manual[key] ?? "";
  el("manual-resume-modal").hidden = false;
};
el("manual-cancel").onclick = () => {
  el("manual-resume-modal").hidden = true;
};
el("manual-save").onclick = () => {
  manual = Object.fromEntries(Object.keys(manualNames).map((key) => [key, input("manual-" + key).value.trim()]));
  el("manual-status").textContent = Object.values(manual).some(Boolean) ? "\u8D44\u6599\u5DF2\u4FDD\u5B58\uFF0C\u53EF\u4E0E\u4E0A\u4F20\u6750\u6599\u4E00\u8D77\u63D0\u4EA4" : "\u53EF\u4E0E\u4E0A\u4F20\u6750\u6599\u4E00\u8D77\u4F7F\u7528";
  el("manual-resume-modal").hidden = true;
};
function resetDraft() {
  files = [];
  manual = {};
  renderFiles();
  input("resume-role").value = "";
  input("resume-extra").value = "";
}
el("web-login-form").onsubmit = (e) => {
  e.preventDefault();
  void post("/login", { code: input("web-code").value }).then(async () => {
    loginDialog.close();
    input("web-code").value = "";
    resetDraft();
    await refresh();
  }).catch((error) => {
    el("web-login-error").textContent = error.message;
  });
};
el("web-logout").onclick = run(async () => {
  await post("/logout");
  loggedIn = false;
  jobs = [];
  current = "";
  lastSnapshot = "";
  metaId = "";
  localStorage.removeItem("dockit_task");
  resetDraft();
  renderTaskState(initialTaskState);
  render();
  loginDialog.showModal();
});
var start = root.querySelector('[data-start="resume"]');
start.onclick = run(async () => {
  if (!input("web-consent").checked) throw new Error("\u8BF7\u5148\u786E\u8BA4\u6750\u6599\u5904\u7406\u8BF4\u660E");
  if (files.length > 6 || files.reduce((n, f) => n + f.size, 0) > 30 * 1024 * 1024) throw new Error("\u6700\u591A 6 \u4E2A\u6587\u4EF6\uFF0C\u603B\u8BA1 30 MB");
  let prompt = `\u76EE\u6807\u5C97\u4F4D\uFF1A${input("resume-role").value}
\u8981\u6C42\uFF1A${input("resume-extra").value}
\u7BC7\u5E45\uFF1A${active("resume-length")}`;
  for (const [key, label] of Object.entries(manualNames)) if (manual[key]) prompt += `

${label}\uFF1A
${manual[key]}`;
  const data = new FormData();
  data.set("user_prompt", prompt);
  data.set("consent", "true");
  data.set("template_id", root.querySelector(".tmpl-card.on").dataset.template);
  data.set("output_format", active("resume-format") === "DOCX+PDF" ? "both" : active("resume-format").toLowerCase());
  data.set("writing_style", active("resume-writing-style"));
  files.forEach((file) => data.append("files", file));
  start.disabled = true;
  try {
    const job = await api("/tasks", { method: "POST", body: data });
    current = job.id;
    localStorage.setItem("dockit_task", current);
    goSub("resume", "run");
    await refresh();
  } finally {
    start.disabled = false;
  }
});
el("web-history").onchange = () => {
  current = input("web-history").value;
  lastSnapshot = "";
  localStorage.setItem("dockit_task", current);
  render();
};
el("submit-answers").onclick = run(async () => {
  const answers = {};
  for (const field of root.querySelectorAll("[data-question-id]")) {
    if (field.required && !field.value.trim()) {
      field.reportValidity();
      return;
    }
    const custom = Array.from(root.querySelectorAll("[data-custom-for]")).find((item) => item.dataset.customFor === field.dataset.questionId);
    answers[field.dataset.questionId] = field.value === "__custom__" ? custom?.value ?? "" : field.value;
  }
  await post(`/tasks/${current}/answers`, answers);
  await refresh();
});
el("clarify-skip").onclick = run(async () => {
  await post(`/tasks/${current}/answers`);
  await refresh();
});
root.addEventListener("click", (e) => {
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "cancel") run(async () => {
    await post(`/tasks/${current}/cancel`);
    await refresh();
  })();
  if (action === "copy-error") run(async () => {
    await navigator.clipboard.writeText(jobs.find((j) => j.id === current)?.error ?? "");
    toast("\u5DF2\u590D\u5236");
  })();
});
function render() {
  if (!jobs.some((job2) => job2.id === current)) current = jobs[0]?.id ?? "";
  const picker = el("web-history");
  if (picker !== document.activeElement) {
    picker.innerHTML = jobs.map((job2) => `<option value="${job2.id}">${new Date(job2.created * 1e3).toLocaleString()} \xB7 ${labels[job2.status]}</option>`).join("");
    picker.value = current;
  }
  const job = jobs.find((j) => j.id === current);
  if (job && JSON.stringify(job) !== lastSnapshot) {
    lastSnapshot = JSON.stringify(job);
    if (metaId !== current) {
      metaId = current;
      setTaskMeta({ tool: "resume", title: "\u7B80\u5386\u751F\u6210", taskId: current, startedAt: (job.started_at ?? job.created) * 1e3 });
    }
    const nextQuestions = current + JSON.stringify(job.questions ?? []);
    if (nextQuestions !== questionKey) {
      questionKey = nextQuestions;
      cachedQuestions = (job.questions ?? []).map((q) => ({ ...q, options: q.options?.map((o) => typeof o === "string" ? o : String(o.label ?? o.value)) }));
    }
    const state = {
      ...initialTaskState,
      status: job.status === "queued" ? "running" : job.status,
      questions: cachedQuestions,
      error: job.error ?? "",
      progress: [job.status === "queued" ? `\u6392\u961F\u4E2D\uFF0C\u524D\u65B9 ${job.queue_position} \u4E2A\u4EFB\u52A1` : job.message],
      qa: { ...initialTaskState.qa, ...job.qa },
      artifacts: []
    };
    renderTaskState(state);
  }
  const elapsed = root.querySelector("#run-resume .bar-meta .mono");
  if (job && elapsed) elapsed.textContent = `~${Math.max(0, Math.round((job.ended_at ?? Date.now() / 1e3) - (job.started_at ?? job.created)))}s`;
  const completed = jobs.filter((j) => j.status === "completed");
  el("art-resume").innerHTML = completed.length ? completed.flatMap((j) => j.artifacts.map((file, i) => {
    const pdf = file.name.endsWith(".pdf");
    return `<div class="vrow"><div class="v-badge ${pdf ? "v-pdf" : "v-docx"}">${pdf ? "P" : "W"}</div><div><div class="v-title">${escapeHtml(file.name)}</div><div class="v-sub">${new Date(j.created * 1e3).toLocaleString()}</div></div><a class="v-open" href="/api/tasks/${j.id}/files/${i}">\u4E0B\u8F7D</a></div>`;
  })).join("") : '<div class="empty">\u6682\u65E0\u4EA7\u7269\uFF0C\u751F\u6210\u540E\u7684\u6587\u4EF6\u4F1A\u51FA\u73B0\u5E76\u4FDD\u7559\u5728\u8FD9\u91CC</div>';
  const badge = root.querySelector('#page-resume [data-sub="art"] .badge');
  if (badge) {
    badge.textContent = String(completed.reduce((n, j) => n + j.artifacts.length, 0));
    badge.hidden = !completed.length;
  }
}
async function refresh() {
  const me = await api("/me");
  loggedIn = true;
  el("web-quota").textContent = me.benchmark_mode ? `\u672C\u673A\u538B\u6D4B \xB7 \u4E0D\u9650\u6B21\u6570 \xB7 ${me.max_concurrent_tasks} \u5E76\u53D1` : `\u5269\u4F59 ${Math.max(0, me.quota - me.used)} \u6B21`;
  el("web-logout").hidden = me.benchmark_mode;
  el("web-retention").textContent = me.retention_days;
  const previous = jobs.find((j) => j.id === current)?.status;
  jobs = await api("/tasks");
  render();
  if (previous && previous !== "completed" && jobs.find((j) => j.id === current)?.status === "completed") goSub("resume", "art");
}
async function poll() {
  try {
    if (loggedIn) await refresh();
  } catch (error) {
    toast(error.message, "warn");
  }
  setTimeout(poll, 2500);
}
refresh().catch((error) => {
  if (!loginDialog.open) toast(error.message, "warn");
}).finally(poll);
