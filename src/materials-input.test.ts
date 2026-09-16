import { afterEach, expect, it, vi } from 'vitest';
import { open } from '@tauri-apps/plugin-dialog';
import { createTaskController } from './taskController';

vi.mock('@tauri-apps/plugin-dialog', () => ({ open: vi.fn() }));
vi.mock('./ui', () => ({ escapeHtml: (s: string) => s, flashSubtab: vi.fn(), goSub: vi.fn(),
  renderTaskState: vi.fn(), setTaskMeta: vi.fn(), toast: vi.fn() }));
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

it('appends selected files, deduplicates, and sends saved manual input alongside them', async () => {
  const nodes = new Map<string, any>();
  const node = (id: string) => {
    if (!nodes.has(id)) nodes.set(id, { value: '', hidden: true, innerHTML: '', textContent: '', dataset: {},
      handlers: {} as Record<string, Function>, focus: vi.fn(),
      addEventListener(event: string, fn: Function) { this.handlers[event] = fn; } });
    return nodes.get(id);
  };
  const start = node('start'); start.dataset.start = 'resume';
  const clicks: Function[] = [];
  vi.stubGlobal('document', { getElementById: node,
    addEventListener: (event: string, fn: Function) => { if(event === 'click') clicks.push(fn); },
    querySelectorAll: (s: string) => s === '[data-start]' ? [start] : [],
    querySelector: (s: string) => s.startsWith('#') && !s.includes(' ') ? node(s.slice(1)) : null });
  vi.stubGlobal('window', { addEventListener: vi.fn(), __TAURI_INTERNALS__: {} });
  const send = vi.fn(async () => {});
  createTaskController(send, { validateSettings: () => '', capabilityEffective: () => true,
    saveFields: vi.fn(), taskProvider: () => ({}) } as any);
  const choose = async (paths: string[]) => {
    vi.mocked(open).mockResolvedValueOnce(paths);
    clicks.forEach(click => click({ target: { closest: (s: string) => s === '[data-pick]' ? { dataset: { pick: 'resume' } } : null } }));
    await Promise.resolve(); await Promise.resolve();
  };
  await choose(['E:/experience.txt', 'E:/photo.png']);
  await choose(['E:/photo.png', 'E:/project.txt']);
  node('resume-manual').handlers.click();
  node('manual-basic').value = '测试同学，意向自动化工程师';
  node('manual-experience').value = '课程项目：完成 PLC 控制台搭建';
  node('manual-save').handlers.click();
  node('resume-manual').handlers.click();
  expect(node('manual-experience').value).toContain('PLC');
  node('manual-experience').value = '未保存的修改';
  node('manual-cancel').handlers.click();
  start.handlers.click();
  const payload = (send.mock.calls[0] as any)[0].payload;
  expect(payload.materials).toEqual(['E:/experience.txt', 'E:/photo.png', 'E:/project.txt']);
  expect(payload.user_prompt).toContain('完成 PLC 控制台搭建');
  expect(payload.user_prompt).toContain('测试同学');
  expect(payload.user_prompt).not.toContain('未保存的修改');
});
