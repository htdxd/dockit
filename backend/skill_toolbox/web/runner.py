"""单个简历任务对应一个独立 Sidecar 进程；只发送服务器构造的固定协议。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from skill_toolbox.unicode_utils import redact_secrets
from .state import Store


class JobRunner:
    def __init__(self, store: Store, config: dict):
        self.store, self.config = store, config
        self.process = None
        self.current = None
        self.cancel_requested = False

    async def cancel(self):
        self.cancel_requested = True
        if self.process is not None:
            await self.send('cancel_task')

    def clean(self, value) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for secret in (self.config['provider'].get('api_key'), self.config.get('mineru_key')):
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return redact_secrets(text)

    async def send(self, kind: str, **payload):
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError('任务进程已退出')
        self.process.stdin.write((json.dumps({'id': self.current, 'type': kind, 'payload': payload}) + '\n').encode())
        await self.process.stdin.drain()

    async def stop(self):
        process = self.process
        if not process or process.returncode is not None:
            return
        try:
            await self.send('cancel_task')
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 15)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            if sys.platform == 'win32':
                await asyncio.to_thread(subprocess.run, ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            elif process.returncode is None:
                process.kill()
            await process.wait()

    async def run(self, job: dict):
        self.current = job['id']
        self.cancel_requested = False
        root = self.store.directory / 'jobs' / job['id']
        output = root / 'outputs'
        output.mkdir(exist_ok=True)
        self.store.update(job['id'], 'running', message='正在准备材料')
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        # 不把 Web 配置文件位置传给领域脚本；模型只见固定领域工具。
        env.pop('DOCKIT_WEB_CONFIG', None)
        with (root / 'stderr.log').open('wb') as stderr:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'skill_toolbox.sidecar', cwd=root, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                limit=4*1024*1024, **({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
            try:
                await self.send('set_mineru_key', mineru_key=self.config['mineru_key'])
                await self.send('start_task', skill_id='resume_pro', tool_mode='domain',
                                provider=self.config['provider'], output_dir=str(output),
                                output_format={'docx':'DOCX','pdf':'PDF','both':'DOCX+PDF'}[job['payload']['output_format']],
                                materials=[str(root / 'uploads' / name) for name in job['payload']['files']],
                                **{key: job['payload'][key] for key in ('user_prompt', 'template_id', 'writing_style')})
                if self.cancel_requested:
                    await self.send('cancel_task')
                async with asyncio.timeout(self.config.get('task_timeout_seconds', 1800)):
                    while line := await self.process.stdout.readline():
                        envelope = json.loads(line)
                        if envelope.get('id') != job['id']:
                            continue
                        event = envelope['event']
                        with (root / 'events.jsonl').open('a', encoding='utf-8') as log:
                            log.write(self.clean({'time': time.time(), **event}) + '\n')
                        if self.event(job['id'], event, output):
                            return
                    raise RuntimeError('任务进程提前退出，请使用任务编号联系管理员。')
            finally:
                await self.stop()
                self.process = None
                self.current = None

    def event(self, job_id: str, event: dict, output: Path) -> bool:
        kind = event.get('type')
        if kind == 'task_completed':
            files = []
            for name in event['artifacts']:
                path = Path(name).resolve()
                if path.parent != output.resolve() or path.suffix.lower() not in ('.docx', '.pdf') or not path.is_file():
                    raise RuntimeError('生成结果未通过下载路径校验')
                files.append({'name': path.name, 'size': path.stat().st_size})
            requested = self.store.get(job_id)['payload']['output_format']
            expected = {'.docx', '.pdf'} if requested == 'both' else {'.' + requested}
            files = [item for item in files if Path(item['name']).suffix in expected]
            if {Path(item['name']).suffix for item in files} != expected:
                raise RuntimeError('生成任务没有交付所选格式的文件')
            self.store.update(job_id, 'completed', message='简历已生成，可以下载', artifacts=files, questions=[])
            return True
        if kind in ('task_failed', 'task_cancelled', 'protocol_error'):
            error = self.clean(event.get('error', '任务已取消'))
            self.store.update(job_id, 'cancelled' if kind == 'task_cancelled' else 'failed',
                              message='任务未完成，次数已返还', error=error, questions=[])
            return True
        if kind == 'questions_requested':
            self.store.update(job_id, 'waiting', questions=event.get('questions', []), message='需要你补充一些经历信息')
        elif kind == 'questions_answered':
            self.store.update(job_id, 'running', questions=[], message='已收到补充信息，继续生成')
        elif kind == 'qa_status':
            self.store.update(job_id, qa=event.get('qa', {}))
        elif kind in ('material_progress', 'model_started', 'tool_started', 'tool_finished'):
            if kind == 'material_progress':
                message = '正在解析材料' if event.get('phase') == 'parsing' else '材料解析完成'
            elif kind == 'model_started':
                message = '正在整理经历与生成内容'
            elif kind == 'tool_finished':
                message = '正在检查和修正生成结果'
            else:
                tool = event.get('tool', '')
                message = {'resume_preview': '正在检查页面观感', 'resume_accept': '正在保存成品',
                           'resume_generate': '正在排版与渲染', 'resume_edit': '正在优化排版',
                           'read_material': '正在阅读材料'}.get(tool, '正在处理简历')
            job = self.store.get(job_id)
            events = [*job['snapshot']['events'], {'time': time.time(), 'message': message}][-40:]
            self.store.update(job_id, message=message, events=events)
        return False
