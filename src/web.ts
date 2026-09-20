import { mountLayout, goSub, renderTaskState, setTaskMeta, toast, escapeHtml } from './ui';
import { initialTaskState } from './state';
import type { Question, TaskState } from './types';

const root = document.getElementById('app')!;
mountLayout(root, { resumeOnly: true });
const el = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const input = (id: string) => el<HTMLInputElement>(id);
const active = (id: string) => el(id).querySelector<HTMLElement>('.on')?.dataset.v ?? '';
let files: File[] = [], jobs: any[] = [], current = localStorage.getItem('dockit_task') ?? '';
let loggedIn = false, lastSnapshot = '', metaId = '';
let questionKey = '', cachedQuestions: Question[] = [];
let manual: Record<string, string> = {};
const manualNames = { basic: '基本信息', education: '教育背景', experience: '项目、工作及其他经历', skills: '技能、成果与补充说明' };
const labels: Record<string, string> = { queued: '排队中', running: '生成中', waiting: '待补充', completed: '已完成', failed: '失败', cancelled: '已取消' };

root.querySelector('.brand-sub')!.textContent = 'AI 简历生成';
root.querySelector('.h-sub')!.textContent = '填信息 → 答少量问题 → 拿到文件';
root.querySelector('.foot')!.innerHTML = '材料与产物保存在服务器；解析与生成会发送至配置的 AI / MinerU 服务。 <a href="https://github.com/htdxd/dockit" target="_blank" rel="noopener">源码 · AGPL-3.0</a>';
root.querySelector('#page-resume .fmt')!.textContent = 'pdf / docx / md / txt / jpg / png / webp · 最多 6 个文件，总计 30 MB';
root.querySelector('#page-resume .btn-note')!.textContent = '自动检查超页 / 溢出 / 层级';
root.querySelector('.hello')!.insertAdjacentHTML('beforeend', '<span id="web-quota"></span><button class="btn-sec" id="web-logout" hidden>退出</button>');
el('sub-resume-new').insertAdjacentHTML('beforeend', '<label class="web-consent"><input id="web-consent" type="checkbox"> 我同意将材料发送至网站配置的 AI / MinerU 服务。材料和任务日志保存 <span id="web-retention">7</span> 天。</label>');
el('sub-resume-run').insertAdjacentHTML('afterbegin', '<select class="inp web-history" id="web-history" aria-label="选择任务"></select>');
root.insertAdjacentHTML('beforeend', '<dialog id="web-login" class="dialog"><form id="web-login-form"><div class="d-head">DocKit Resume</div><p class="d-sub">输入个人邀请码，查看你的任务和成品。请勿共享邀请码。</p><input class="inp" id="web-code" aria-label="邀请码" autocomplete="off" required><p id="web-login-error"></p><div class="d-foot"><button class="d-ok">进入</button></div></form></dialog>');
const loginDialog = el<HTMLDialogElement>('web-login');
loginDialog.oncancel = (event) => event.preventDefault();

async function api(path: string, options: RequestInit = {}) {
  const response = await fetch('/api' + path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) { loggedIn = false; if (!loginDialog.open) loginDialog.showModal(); }
    throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请重试');
  }
  return data;
}
const post = (path: string, data: unknown = {}) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
const run = (fn: () => Promise<void>) => () => void fn().catch((error) => toast(error.message, 'warn'));
function renderFiles() {
  el('materials-list-resume').innerHTML = files.map((file, index) => `<div class="frow">📎 ${escapeHtml(file.name)} <span class="fsz">${Math.round(file.size / 1024)} KB</span><button type="button" class="fx" data-remove-file="${index}">×</button></div>`).join('');
}
function addFiles(selected: File[]) {
  for (const file of selected) if (!files.some((f) => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified)) files.push(file);
  renderFiles();
}
const fileInput = input('materials-input-resume');
fileInput.accept = '.pdf,.docx,.txt,.md,.png,.jpg,.jpeg,.webp';
fileInput.onchange = () => { addFiles(Array.from(fileInput.files ?? [])); fileInput.value = ''; };
const upload = root.querySelector<HTMLElement>('[data-pick="resume"]')!;
upload.onclick = () => fileInput.click();
upload.onkeydown = (e) => { if (['Enter', ' '].includes(e.key)) { e.preventDefault(); fileInput.click(); } };
upload.ondragover = (e) => e.preventDefault();
upload.ondrop = (e) => { e.preventDefault(); addFiles(Array.from(e.dataTransfer?.files ?? [])); };
root.addEventListener('click', (e) => {
  const target = (e.target as HTMLElement).closest<HTMLElement>('[data-remove-file]');
  if (target) { files.splice(Number(target.dataset.removeFile), 1); renderFiles(); }
});
el('resume-manual').onclick = () => {
  for (const key of Object.keys(manualNames)) input('manual-' + key).value = manual[key] ?? '';
  el('manual-resume-modal').hidden = false;
};
el('manual-cancel').onclick = () => { el('manual-resume-modal').hidden = true; };
el('manual-save').onclick = () => {
  manual = Object.fromEntries(Object.keys(manualNames).map((key) => [key, input('manual-' + key).value.trim()]));
  el('manual-status').textContent = Object.values(manual).some(Boolean) ? '资料已保存，可与上传材料一起提交' : '可与上传材料一起使用';
  el('manual-resume-modal').hidden = true;
};
function resetDraft() { files = []; manual = {}; renderFiles(); input('resume-role').value = ''; input('resume-extra').value = ''; }
el('web-login-form').onsubmit = (e) => {
  e.preventDefault();
  void post('/login', { code: input('web-code').value }).then(async () => { loginDialog.close(); input('web-code').value = ''; resetDraft(); await refresh(); })
    .catch((error) => { el('web-login-error').textContent = error.message; });
};
el('web-logout').onclick = run(async () => {
  await post('/logout'); loggedIn = false; jobs = []; current = ''; lastSnapshot = ''; metaId = '';
  localStorage.removeItem('dockit_task'); resetDraft(); renderTaskState(initialTaskState); render(); loginDialog.showModal();
});
const start = root.querySelector<HTMLButtonElement>('[data-start="resume"]')!;
start.onclick = run(async () => {
  if (!input('web-consent').checked) throw new Error('请先确认材料处理说明');
  if (files.length > 6 || files.reduce((n, f) => n + f.size, 0) > 30 * 1024 * 1024) throw new Error('最多 6 个文件，总计 30 MB');
  let prompt = `目标岗位：${input('resume-role').value}\n要求：${input('resume-extra').value}\n篇幅：${active('resume-length')}`;
  for (const [key, label] of Object.entries(manualNames)) if (manual[key]) prompt += `\n\n${label}：\n${manual[key]}`;
  const data = new FormData();
  data.set('user_prompt', prompt); data.set('consent', 'true');
  data.set('template_id', root.querySelector<HTMLElement>('.tmpl-card.on')!.dataset.template!);
  data.set('output_format', active('resume-format') === 'DOCX+PDF' ? 'both' : active('resume-format').toLowerCase());
  data.set('writing_style', active('resume-writing-style'));
  files.forEach((file) => data.append('files', file)); start.disabled = true;
  try { const job = await api('/tasks', { method: 'POST', body: data }); current = job.id; localStorage.setItem('dockit_task', current); goSub('resume', 'run'); await refresh(); }
  finally { start.disabled = false; }
});
el('web-history').onchange = () => { current = input('web-history').value; lastSnapshot = ''; localStorage.setItem('dockit_task', current); render(); };
el('submit-answers').onclick = run(async () => {
  const answers: Record<string, string> = {};
  for (const field of root.querySelectorAll<HTMLInputElement>('[data-question-id]')) {
    if (field.required && !field.value.trim()) { field.reportValidity(); return; }
    const custom = Array.from(root.querySelectorAll<HTMLInputElement>('[data-custom-for]')).find((item) => item.dataset.customFor === field.dataset.questionId);
    answers[field.dataset.questionId!] = field.value === '__custom__' ? custom?.value ?? '' : field.value;
  }
  await post(`/tasks/${current}/answers`, answers); await refresh();
});
el('clarify-skip').onclick = run(async () => { await post(`/tasks/${current}/answers`); await refresh(); });
root.addEventListener('click', (e) => {
  const action = (e.target as HTMLElement).closest<HTMLElement>('[data-action]')?.dataset.action;
  if (action === 'cancel') run(async () => { await post(`/tasks/${current}/cancel`); await refresh(); })();
  if (action === 'copy-error') run(async () => { await navigator.clipboard.writeText(jobs.find((j) => j.id === current)?.error ?? ''); toast('已复制'); })();
});
function render() {
  if (!jobs.some((job) => job.id === current)) current = jobs[0]?.id ?? '';
  const picker = el<HTMLSelectElement>('web-history');
  if (picker !== document.activeElement) { picker.innerHTML = jobs.map((job) => `<option value="${job.id}">${new Date(job.created * 1000).toLocaleString()} · ${labels[job.status]}</option>`).join(''); picker.value = current; }
  const job = jobs.find((j) => j.id === current);
  if (job && JSON.stringify(job) !== lastSnapshot) {
    lastSnapshot = JSON.stringify(job);
    if (metaId !== current) { metaId = current; setTaskMeta({ tool: 'resume', title: '简历生成', taskId: current, startedAt: (job.started_at ?? job.created) * 1000 }); }
    const nextQuestions = current + JSON.stringify(job.questions ?? []);
    if (nextQuestions !== questionKey) {
      questionKey = nextQuestions;
      cachedQuestions = (job.questions ?? []).map((q: any) => ({ ...q, options: q.options?.map((o: any) => typeof o === 'string' ? o : String(o.label ?? o.value)) }));
    }
    const state: TaskState = { ...initialTaskState, status: job.status === 'queued' ? 'running' : job.status, questions: cachedQuestions,
      error: job.error ?? '', progress: [job.status === 'queued' ? `排队中，前方 ${job.queue_position} 个任务` : job.message],
      qa: { ...initialTaskState.qa, ...job.qa }, artifacts: [] };
    renderTaskState(state);
  }
  const elapsed = root.querySelector<HTMLElement>('#run-resume .bar-meta .mono');
  if (job && elapsed) elapsed.textContent = `~${Math.max(0, Math.round((job.ended_at ?? Date.now()/1000) - (job.started_at ?? job.created)))}s`;
  const completed = jobs.filter((j) => j.status === 'completed');
  el('art-resume').innerHTML = completed.length ? completed.flatMap((j) => j.artifacts.map((file: any, i: number) => {
    const pdf = file.name.endsWith('.pdf');
    return `<div class="vrow"><div class="v-badge ${pdf ? 'v-pdf' : 'v-docx'}">${pdf ? 'P' : 'W'}</div><div><div class="v-title">${escapeHtml(file.name)}</div><div class="v-sub">${new Date(j.created * 1000).toLocaleString()}</div></div><a class="v-open" href="/api/tasks/${j.id}/files/${i}">下载</a></div>`;
  })).join('') : '<div class="empty">暂无产物，生成后的文件会出现并保留在这里</div>';
  const badge = root.querySelector<HTMLElement>('#page-resume [data-sub="art"] .badge');
  if (badge) { badge.textContent = String(completed.reduce((n,j) => n+j.artifacts.length,0)); badge.hidden = !completed.length; }
}
async function refresh() {
  const me = await api('/me'); loggedIn = true;
  el('web-quota').textContent = me.benchmark_mode ? `本机压测 · 不限次数 · ${me.max_concurrent_tasks} 并发` : `剩余 ${Math.max(0, me.quota - me.used)} 次`;
  el('web-logout').hidden = me.benchmark_mode;
  el('web-retention').textContent = me.retention_days;
  const previous = jobs.find((j) => j.id === current)?.status;
  jobs = await api('/tasks'); render();
  if (previous && previous !== 'completed' && jobs.find((j) => j.id === current)?.status === 'completed') goSub('resume', 'art');
}
async function poll() {
  try { if (loggedIn) await refresh(); } catch (error) { toast((error as Error).message, 'warn'); }
  setTimeout(poll, 2500);
}
refresh().catch((error) => { if (!loginDialog.open) toast(error.message, 'warn'); }).finally(poll);
