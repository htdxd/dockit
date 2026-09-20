"""本机分档压测。默认只展示计划；--execute 才运行，generate 会消耗 API 额度。"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx
import psutil


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else None


def summarize(rows, elapsed):
    successes = [row for row in rows if row['ok']]
    durations = [row['seconds'] for row in successes]
    return {'requests': len(rows), 'succeeded': len(successes), 'failed': len(rows)-len(successes),
            'success_rate': len(successes)/len(rows) if rows else 0,
            'elapsed_seconds': elapsed, 'throughput_per_hour': len(successes)/elapsed*3600 if elapsed else 0,
            'p50_seconds': statistics.median(durations) if durations else None,
            'p95_seconds': percentile(durations,.95),
            'mean_queue_seconds': statistics.mean([r.get('queue_seconds',0) for r in successes]) if successes else None,
            'mean_generation_seconds': statistics.mean([r.get('generation_seconds',0) for r in successes]) if successes else None}


def check_artifact(content: bytes, filename: str, marker: str):
    if filename.lower().endswith('.docx'):
        from lxml import etree
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() is not None:
                raise ValueError('DOCX ZIP 校验失败')
            tree = etree.fromstring(archive.read('word/document.xml'))
            text = ''.join(tree.xpath('//w:t/text()', namespaces={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}))
    else:
        import pymupdf
        with pymupdf.open(stream=content, filetype='pdf') as pdf:
            if len(pdf) < 1:
                raise ValueError('PDF 没有页面')
            text = ''.join(page.get_text() for page in pdf)
    if marker not in text:
        raise ValueError('产物中未找到本任务姓名标识：可能串单或内容缺失')


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


async def sample_resources(stop, rows, concurrency, pid):
    psutil.cpu_percent()
    while not stop.is_set():
        memory = psutil.virtual_memory()
        word_pids, word_rss = [], 0
        for process in psutil.process_iter(['name','memory_info']):
            try:
                if (process.info['name'] or '').lower() == 'winword.exe':
                    word_pids.append(process.pid)
                    if process.info['memory_info'] is not None:
                        word_rss += process.info['memory_info'].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            server = psutil.Process(pid)
            tree_rss = sum(p.memory_info().rss for p in [server,*server.children(recursive=True)] if p.is_running())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            tree_rss = 0
        rows.append({'time':time.time(),'concurrency':concurrency,'cpu_percent':psutil.cpu_percent(),
                     'ram_used_gb':memory.used/1024**3,'ram_available_gb':memory.available/1024**3,
                     'server_tree_rss_mb':tree_rss/1024**2,'word_count':len(word_pids),
                     'word_rss_mb':word_rss/1024**2,'word_pids':';'.join(map(str,word_pids))})
        try:
            await asyncio.wait_for(stop.wait(),2)
        except TimeoutError:
            pass


async def generate_one(client, number, template, output_format, materials, timeout):
    marker = f'测{number:04d}'
    result = {'number':number,'template':template,'ok':False,'job_id':'','questions':0}
    started = time.perf_counter()
    prompt = (f'姓名：{marker}。邮箱：bench@example.com。求职：Python 后端开发。'
              '2020.09–2024.06 示例大学软件工程本科。2024.03–2024.06 独立完成个人项目“图书借阅系统”，'
              '使用 Python、FastAPI、SQLite 实现检索、借阅归还和逾期提醒，编写 28 条接口测试，完成部署说明。'
              '熟悉 Python、SQL、Git，了解 Linux。请生成一页中文简历，不显示照片。'
              '这是合成压测资料，姓名必须原样保留。未提供的信息不要编造，没有其他补充信息。')
    try:
        identity = await client.get('/api/me')  # 自动会话，不需要邀请码。
        identity.raise_for_status()
        uploads = [('files',(path.name,path.read_bytes())) for path in materials]
        response = await client.post('/api/tasks', data={'user_prompt':prompt,'template_id':template,
            'output_format':output_format,'writing_style':'balanced','consent':'true'}, files=uploads or None)
        response.raise_for_status()
        result['job_id'] = job_id = response.json()['id']
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            response = await client.get('/api/tasks/'+job_id)
            response.raise_for_status()
            job = response.json()
            if job['status']=='waiting':
                answered = await client.post('/api/tasks/'+job_id+'/answers',json={})
                answered.raise_for_status()
                result['questions'] += 1
            if job['status'] in ('completed','failed','cancelled'):
                result.update(status=job['status'],error=job.get('error') or '',
                              queue_seconds=job.get('started_at',job['created'])-job['created'],
                              generation_seconds=job.get('ended_at',time.time())-job.get('started_at',job['created']))
                if job['status']=='completed':
                    if not job['artifacts']:
                        raise ValueError('completed 但没有产物')
                    for index, artifact in enumerate(job['artifacts']):
                        downloaded = await client.get(f'/api/tasks/{job_id}/files/{index}')
                        downloaded.raise_for_status()
                        check_artifact(downloaded.content,artifact['name'],marker)
                    result['ok'] = True
                break
            await asyncio.sleep(2)
        else:
            await client.post('/api/tasks/'+job_id+'/cancel',json={})
            raise TimeoutError('压测客户端等待超时，已请求取消任务')
    except Exception as exc:
        # HTTP 错误只记状态和路径，不复制供应商配置或密钥。
        result['error'] = f'{type(exc).__name__}: {exc}'
        if result['job_id'] and result.get('status') not in ('completed','failed','cancelled'):
            try:
                cancelled = await client.post('/api/tasks/'+result['job_id']+'/cancel',json={})
                cancelled.raise_for_status()
            except httpx.HTTPError as cancel_error:
                result['cancel_error'] = str(cancel_error)
    result['seconds'] = time.perf_counter()-started
    return result


async def stage(config, args, concurrency, directory):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    directory.mkdir(parents=True)
    config = {**config, 'port':port,'public_origin':f'http://127.0.0.1:{port}', 'secure_cookie':False,
              'benchmark_mode':True, 'max_concurrent_tasks':concurrency,
              'data_dir':str(directory/'data'), 'queue_limit':max(20,concurrency*2)}
    rows, resources = [], []
    with tempfile.TemporaryDirectory(prefix='dockit-bench-config-') as temporary:
        private_config = Path(temporary)/'server.json'
        private_config.write_text(json.dumps(config),encoding='utf-8')
        with (directory/'server-console.log').open('wb') as log:
            process = subprocess.Popen([sys.executable,'-m','skill_toolbox.web','--config',str(private_config)],
                stdout=log,stderr=log,env={**os.environ,'PYTHONUTF8':'1'},
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
            stop = asyncio.Event()
            sampler = asyncio.create_task(sample_resources(stop,resources,concurrency,process.pid))
            try:
                async with httpx.AsyncClient(base_url=config['public_origin'],timeout=60,trust_env=False,
                                           headers={'Origin':config['public_origin']}) as health:
                    deadline = time.monotonic()+90
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('网站未启动，查看 server-console.log')
                        try:
                            if (await health.get('/healthz')).status_code==200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic()>deadline:
                            raise TimeoutError('网站启动超时')
                        await asyncio.sleep(.5)
                started = time.perf_counter()
                async def user(index):
                    async with httpx.AsyncClient(base_url=config['public_origin'],timeout=60,trust_env=False,
                                                headers={'Origin':config['public_origin']}) as client:
                        if args.mode == 'http':
                            identity = await client.get('/api/me')
                            identity.raise_for_status()
                        for round_id in range(args.rounds):
                            number = index*args.rounds+round_id+1
                            if args.mode=='generate':
                                template = ['t001','t109'][number%2] if args.template=='mixed' else args.template
                                row = await generate_one(client,number,template,args.format,args.material,args.timeout)
                            else:
                                tick = time.perf_counter()
                                try:
                                    response = await client.get('/api/tasks')
                                    response.raise_for_status()
                                    row = {'ok':True}
                                except httpx.HTTPError as exc:
                                    row = {'ok':False,'error':str(exc)}
                                row['seconds'] = time.perf_counter()-tick
                            row['concurrency'] = concurrency
                            rows.append(row)
                            print(f"level={concurrency} finished={len(rows)}/{concurrency*args.rounds} ok={row['ok']}",flush=True)
                await asyncio.gather(*(user(index) for index in range(concurrency)))
                summary = {'concurrency':concurrency,**summarize(rows,time.perf_counter()-started)}
            finally:
                stop.set()
                await sampler
                # 只结束本次脚本创建的服务器树，不扫描/杀死系统中所有 Word。
                if process.poll() is None:
                    if sys.platform=='win32':
                        await asyncio.to_thread(subprocess.run,['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        process.terminate()
                    await asyncio.to_thread(process.wait,30)
                write_csv(directory/'results.csv',rows)
                write_csv(directory/'resources.csv',resources)
                (directory/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    summary.update(peak_cpu_percent=max((r['cpu_percent'] for r in resources),default=0),
                   min_available_ram_gb=min((r['ram_available_gb'] for r in resources),default=0),
                   peak_word_processes=max((r['word_count'] for r in resources),default=0))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='server.json')
    parser.add_argument('--mode',choices=['http','generate'],default='generate')
    parser.add_argument('--levels',default='1,2,4')
    parser.add_argument('--rounds',type=int,default=2,help='每档每个并发用户执行的任务/请求数')
    parser.add_argument('--template',choices=['mixed','t001','t109'],default='mixed')
    parser.add_argument('--format',choices=['docx','pdf','both'],default='both')
    parser.add_argument('--material',type=Path,action='append',default=[])
    parser.add_argument('--timeout',type=int,default=2100)
    parser.add_argument('--output',type=Path,default=Path('loadtest-results'))
    parser.add_argument('--execute',action='store_true')
    args = parser.parse_args()
    levels = [int(n) for n in args.levels.split(',')]
    if not levels or any(n<1 or n>32 for n in levels) or args.rounds<1:
        parser.error('并发档位须为 1–32，rounds 须大于 0')
    print(f'Mode={args.mode}; levels={levels}; total={sum(levels)*args.rounds}; real_API_cost={args.mode=="generate"}')
    if not args.execute:
        print('仅展示计划。加 --execute 才会启动压测；generate 会调用真实 LLM/MinerU 并计费。')
        return
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    output = (args.output / time.strftime('%Y%m%d-%H%M%S')).resolve()
    output.mkdir(parents=True)
    summaries = []
    async def run():
        for index, concurrency in enumerate(levels):
            summary = await stage(config,args,concurrency,output/f'{index+1}-c{concurrency}')
            summaries.append(summary)
            write_csv(output/'summary.csv',summaries)
    try:
        asyncio.run(run())
    finally:
        report = {'mode':args.mode,'cpu_logical':psutil.cpu_count(),'cpu_physical':psutil.cpu_count(logical=False),
                  'ram_gb':psutil.virtual_memory().total/1024**3,'platform':platform.platform(),
                  'model':config.get('provider',{}).get('model'),'summaries':summaries,
                  'notes':['HTTP 模式只测读取接口，不代表简历生成能力。','失败任务不计入成功耗时分位数；必须同时查看失败率。',
                           '小样本 P95 不稳定；任务日志留在各档 data/jobs 下。','Word 进程统计为整机值，请关闭其他 Word 文档后测。']}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Report:',output,flush=True)


if __name__=='__main__':
    main()
