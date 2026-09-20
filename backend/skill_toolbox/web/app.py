from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
import shutil
import time
import traceback
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .runner import JobRunner
from .state import ACTIVE, Store

MAX_UPLOAD = 30 * 1024 * 1024
EXTENSIONS = {'.pdf', '.docx', '.txt', '.md', '.jpg', '.jpeg', '.png', '.webp'}


class Login(BaseModel):
    code: str = Field(min_length=1, max_length=100)


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_prompt: str = Field(default='', max_length=20000)
    template_id: str = 't109'
    output_format: str = 'docx'
    writing_style: str = 'balanced'
    consent: bool = False


def validate_upload(path: Path):
    if path.suffix == '.docx':
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 100*1024*1024:
                    raise ValueError('DOCX 解压后超过 100 MB')
                if 'word/document.xml' not in archive.namelist():
                    raise ValueError('不是有效的 DOCX 文件')
                if any('vbaproject' in name.lower() for name in archive.namelist()):
                    raise ValueError('不接受包含宏的文档')
        except zipfile.BadZipFile:
            raise ValueError('DOCX 文件损坏') from None
    elif path.suffix == '.pdf':
        import pymupdf
        try:
            with pymupdf.open(path) as doc:
                if doc.needs_pass or not 1 <= len(doc) <= 20:
                    raise ValueError('PDF 必须未加密且为 1–20 页')
        except RuntimeError:
            raise ValueError('PDF 文件损坏') from None
    elif path.suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
        from PIL import Image
        try:
            with Image.open(path) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError('图片超过 2500 万像素，请先缩小')
                image.verify()
        except (OSError, Image.DecompressionBombError):
            raise ValueError('图片无法读取或尺寸过大') from None


def create_app(config: dict, *, runner_factory=JobRunner) -> FastAPI:
    store = Store(Path(config['data_dir']))
    concurrency = int(config.get('max_concurrent_tasks', 1))
    if not 1 <= concurrency <= 32:
        raise ValueError('max_concurrent_tasks 必须为 1–32')
    runners = [runner_factory(store, config) for _ in range(concurrency)]
    runner = runners[0]
    benchmark = bool(config.get('benchmark_mode', False))
    wake = asyncio.Event()
    attempts: dict[str, list[float]] = {}
    public_origin = config.get('public_origin', 'http://127.0.0.1:8080').rstrip('/')

    def purge():
        cutoff = time.time() - config.get('retention_days', 7)*86400
        for job in store.jobs():
            if job['created'] < cutoff and job['status'] not in ACTIVE:
                path = (store.directory / 'jobs' / job['id']).resolve()
                path.relative_to(store.directory / 'jobs')
                if path.exists():
                    shutil.rmtree(path)
                with store.db() as db:
                    db.execute('DELETE FROM feedback WHERE job=?', (job['id'],))
                    db.execute('DELETE FROM jobs WHERE id=?', (job['id'],))
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))

    async def queue_loop(worker):
        while True:
            purge()
            jobs = [job for job in reversed(store.jobs()) if job['status'] == 'queued']
            if not jobs:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), 3600)
                except TimeoutError:
                    pass
                continue
            job = jobs[0]
            # 认领与状态写入之间不 await；单个事件循环内不会重复领取同一任务。
            store.update(job['id'], 'running')
            try:
                await worker.run(job)
            except asyncio.CancelledError:
                store.update(job['id'], 'failed', error='服务器已停止，次数已返还，请重新提交。', questions=[])
                raise
            except Exception as exc:
                error = runner.clean(str(exc)) or '任务超时，请重试或联系管理员。'
                store.update(job['id'], 'failed', error=error, questions=[], message='任务失败，次数已返还')
                with (store.directory / 'jobs' / job['id'] / 'server-error.log').open('a', encoding='utf-8') as log:
                    log.write(f'{time.time()} {type(exc).__name__}: {error}\n')

    @asynccontextmanager
    async def lifespan(app):
        for job in store.jobs():
            if job['status'] in ('running', 'waiting'):
                store.update(job['id'], 'failed', error='服务器重启中断了任务，次数已返还。', questions=[])
        queues = [asyncio.create_task(queue_loop(worker)) for worker in runners]
        try:
            yield
        finally:
            for queue in queues:
                queue.cancel()
            await asyncio.gather(*queues, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runner = store, runner
    app.state.runners = runners

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if benchmark and request.client.host not in ('127.0.0.1', '::1'):
            return JSONResponse({'detail': '压测模式只允许本机访问'}, status_code=403)
        if request.method in ('POST', 'DELETE'):
            if request.headers.get('origin') != public_origin:
                logging.getLogger(__name__).warning('Rejected request origin: %s', request.url.path)
                return JSONResponse({'detail': '请求来源不匹配'}, status_code=403)
            length = request.headers.get('content-length', '')
            if not length.isdigit() or int(length) > MAX_UPLOAD + 1024*1024:
                logging.getLogger(__name__).warning('Rejected request size: %s', request.url.path)
                return JSONResponse({'detail': '请求过大或缺少长度信息'}, status_code=413)
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger(__name__).error('%s %s: %s', request.method, request.url.path,
                                             runner.clean(traceback.format_exc()))
            response = JSONResponse({'detail': '服务器处理失败，错误已记录，请稍后重试。'}, status_code=500)
        if response.status_code >= 400:
            logging.getLogger(__name__).warning('%s %s status=%s', request.method, request.url.path, response.status_code)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"
        return response

    def owner(request: Request) -> str:
        result = store.owner(request.cookies.get('dockit_session', ''))
        if not result:
            raise HTTPException(401, '请输入邀请码后继续')
        return result

    def owned(request: Request, job_id: str) -> dict:
        job = store.get(job_id, owner(request))
        if not job:
            raise HTTPException(404, '任务不存在或无权访问')
        return job

    def public(job: dict) -> dict:
        queued = [j['id'] for j in reversed(store.jobs()) if j['status'] in ACTIVE]
        return {'id': job['id'], 'status': job['status'], 'created': job['created'],
                'queue_position': queued.index(job['id']) if job['id'] in queued else 0,
                **job['snapshot']}

    @app.get('/healthz')
    async def health():
        return {'ok': True}

    @app.post('/api/login')
    async def login(request: Request, form: Login):
        ip = request.client.host
        now = time.time()
        for key in list(attempts):
            attempts[key] = [t for t in attempts[key] if t > now-600]
            if not attempts[key]:
                del attempts[key]
        history = attempts.setdefault(ip, [])
        if len(history) >= 10:
            raise HTTPException(429, '尝试次数过多，请十分钟后再试')
        history.append(now)
        token = store.login(form.code)
        if not token:
            raise HTTPException(401, '邀请码不正确')
        response = JSONResponse({'ok': True})
        response.set_cookie('dockit_session', token, httponly=True, samesite='strict',
                            secure=config.get('secure_cookie', True), max_age=7*86400)
        return response

    @app.post('/api/logout')
    async def logout(request: Request):
        from .state import digest
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('dockit_session', '')),))
        response = JSONResponse({'ok': True})
        response.delete_cookie('dockit_session')
        return response

    @app.get('/api/me')
    async def me(request: Request):
        token = None
        if benchmark and not store.owner(request.cookies.get('dockit_session', '')):
            token = store.login(store.invite(1))
            user = store.owner(token)
        else:
            user = owner(request)
        response = JSONResponse({**store.quota(user), 'retention_days': config.get('retention_days', 7),
                                 'benchmark_mode': benchmark, 'max_concurrent_tasks': concurrency})
        if token:
            response.set_cookie('dockit_session', token, httponly=True, samesite='strict', secure=False)
        return response

    @app.get('/api/tasks')
    async def tasks(request: Request):
        return [public(job) for job in store.jobs(owner(request))]

    @app.post('/api/tasks')
    async def create_task(request: Request):
        user = owner(request)
        async with request.form(max_files=6, max_fields=8, max_part_size=25000) as form:
            try:
                fields = Submission.model_validate({key: value for key, value in form.multi_items() if key != 'files'})
            except ValueError:
                raise HTTPException(400, '提交字段不合法或内容过长') from None
            if fields.template_id not in ('t001', 't109') or fields.output_format not in ('docx', 'pdf', 'both') or fields.writing_style not in ('light', 'balanced', 'strong'):
                raise HTTPException(400, '请选择支持的模板、格式和写作档位')
            if not fields.consent:
                raise HTTPException(400, '请确认材料处理说明')
            files = form.getlist('files')
            if not files and not fields.user_prompt.strip():
                raise HTTPException(400, '请上传简历材料或填写经历')
            job_id = secrets.token_hex(16)
            root = store.directory / 'jobs' / job_id
            uploads = root / 'uploads'
            uploads.mkdir(parents=True)
            names, total = [], 0
            try:
                for index, upload in enumerate(files):
                    if not hasattr(upload, 'filename'):
                        raise ValueError('文件字段不合法')
                    ext = Path(upload.filename or '').suffix.lower()
                    if ext not in EXTENSIONS:
                        raise ValueError('仅支持 PDF、DOCX、TXT、MD 和常见图片')
                    name = f'{index+1}{ext}'
                    path = uploads / name
                    with path.open('wb') as target:
                        while chunk := await upload.read(1024*1024):
                            total += len(chunk)
                            if total > MAX_UPLOAD:
                                raise ValueError('材料总大小不能超过 30 MB')
                            target.write(chunk)
                    await asyncio.to_thread(validate_upload, path)
                    names.append(name)
                store.create(user, job_id, {**fields.model_dump(exclude={'consent'}), 'files': names},
                             config.get('queue_limit', 20), concurrency=concurrency, unlimited=benchmark)
            except Exception as exc:
                shutil.rmtree(root)
                if isinstance(exc, ValueError):
                    raise HTTPException(400, str(exc)) from None
                raise
            wake.set()
            return {'id': job_id}

    @app.get('/api/tasks/{job_id}')
    async def task(request: Request, job_id: str):
        return public(owned(request, job_id))

    @app.post('/api/tasks/{job_id}/answers')
    async def answer(request: Request, job_id: str):
        job = owned(request, job_id)
        worker = next((r for r in runners if r.current == job_id), None)
        if job['status'] != 'waiting' or worker is None:
            raise HTTPException(409, '此任务当前未等待补充信息')
        values = await request.json()
        allowed = {str(q['id']) for q in job['snapshot']['questions']}
        if not isinstance(values, dict) or not set(values) <= allowed or any(not isinstance(v, str) or len(v)>10000 for v in values.values()):
            raise HTTPException(400, '回答字段不合法')
        store.update(job_id, 'running', questions=[], message='正在提交补充信息')
        try:
            await worker.send('answer_questions', answers=values)
        except RuntimeError:
            store.update(job_id, 'waiting', questions=job['snapshot']['questions'])
            raise HTTPException(409, '任务暂时无法接收回答，请刷新后重试') from None
        return {'ok': True}

    @app.post('/api/tasks/{job_id}/cancel')
    async def cancel(request: Request, job_id: str):
        job = owned(request, job_id)
        if job['status'] == 'queued':
            store.update(job_id, 'cancelled', message='任务已取消，次数已返还')
        else:
            worker = next((r for r in runners if r.current == job_id), None)
            if worker is not None:
                await worker.cancel()
        return {'ok': True}

    @app.get('/api/tasks/{job_id}/files/{index}')
    async def download(request: Request, job_id: str, index: int):
        job = owned(request, job_id)
        files = job['snapshot']['artifacts']
        if job['status'] != 'completed' or not 0 <= index < len(files):
            raise HTTPException(404, '产物不存在')
        path = store.directory / 'jobs' / job_id / 'outputs' / files[index]['name']
        return FileResponse(path, filename=path.name)

    @app.post('/api/tasks/{job_id}/feedback')
    async def feedback(request: Request, job_id: str):
        owned(request, job_id)
        value = await request.json()
        if value.get('rating') not in (1, 2, 3, 4, 5) or not isinstance(value.get('text', ''), str) or len(value.get('text', '')) > 2000:
            raise HTTPException(400, '请填写 1–5 分及不超过 2000 字的建议')
        store.feedback(job_id, value['rating'], value.get('text', ''))
        return {'ok': True}

    app.mount('/', StaticFiles(directory=Path(__file__).parent / 'static', html=True), name='website')
    return app
