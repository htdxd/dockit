"""隔离进程执行真实 Word 检查，不调用 LLM 或 MinerU 云端。"""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback


def probe(stage: str):
    from skill_toolbox.word_com import check_registered_word, start_word, close_word
    if stage == 'identity':
        registration = check_registered_word()
        word = start_word()
        try:
            return {'registration':registration, 'name':str(word.Name), 'version':str(word.Version),
                    'application_path':str(word.Path)}
        finally:
            close_word(word)
    from skill_toolbox.resume_layout import pipeline, spacing
    from skill_toolbox.resume_layout import t001
    template = Path(__file__).resolve().parents[1]/'skill_defs/resume_pro/templates'/stage/'template.docx'
    with tempfile.TemporaryDirectory(prefix='dockit-word-check-') as temporary:
        work = Path(temporary)
        if stage == 't109':
            spacing.ensure_archive(template, work/'spacing.json')
        else:
            t001.ensure_archive(template, work/'spacing.json')
        scenario = {'header':{'fields':{'name':'环境校验'}}, 'sections':[{'id':'education', 'entries':[
            {'id':'probe', 'text':'环境测量校验：测试大学 软件工程 本科\n完成文档排版和文字高度测量。'}]}]}
        pipeline.measure_scenario(scenario,work,template=template,template_id=stage)
        measurement = json.loads((work/'measure_result.json').read_text(encoding='utf-8'))['results'][0]
        if measurement['wrapped_lines'] < 1 or measurement['text_height_pt'] <= 0:
            raise RuntimeError('未能获得有效文字高度')
        pdf = work/'preview.pdf'
        word = start_word()
        doc = None
        try:
            doc = word.Documents.Open(str(work/'measure-0.docx'),False,True)
            doc.ExportAsFixedFormat(str(pdf),17)
        finally:
            close_word(word,doc)
        import pymupdf
        with pymupdf.open(pdf) as document:
            text = ''.join(page.get_text() for page in document)
            if '环境测量校验' not in text:
                raise RuntimeError('导出 PDF 未包含替换后的测试正文，不能确认现代文本框正确渲染')
            pages = len(document)
        subprocess.run([shutil.which('pdftoppm'),'-f','1','-singlefile','-png','-r','72',str(pdf),str(work/'page')],
                       capture_output=True,check=True,timeout=60)
        from PIL import Image
        with Image.open(work/'page.png') as image:
            image.verify()
        return {'wrapped_lines':measurement['wrapped_lines'],'text_height_pt':measurement['text_height_pt'],
                'pdf_pages':pages,'text_verified':True,'png_verified':True}


def run_check(report_path: Path, config: dict) -> bool:
    report = {'time':time.time(),'platform':platform.platform(),'python':sys.version.split()[0],
              'paid_api_called':False,'checks':[], 'credentials':{
                  'llm_configured':bool(config.get('provider',{}).get('api_key')) and not str(config.get('provider',{}).get('api_key')).startswith('REPLACE_'),
                  'mineru_configured':bool(config.get('mineru_key')) and not str(config.get('mineru_key')).startswith('REPLACE_'),
                  'note':'仅检查配置是否填写，不验证云端有效性或模型能力。'}}
    from skill_toolbox.tools import resolve_mineru_cli
    for name, command in [('MinerU CLI',None),('Poppler',shutil.which('pdftoppm'))]:
        try:
            args = [*resolve_mineru_cli(),'--version'] if name == 'MinerU CLI' else [command,'-v']
            if not args[0]:
                raise RuntimeError('找不到程序，请使用包内 start-web.bat 启动')
            result = subprocess.run(args,capture_output=True,timeout=30,check=True)
            report['checks'].append({'name':name,'ok':True,'detail':(result.stdout+result.stderr).decode('utf-8',errors='replace')[:500]})
        except Exception as exc:
            report['checks'].append({'name':name,'ok':False,'error':str(exc)})
    for stage in ('identity','t001','t109'):
        if stage != 'identity' and any(not item['ok'] for item in report['checks'] if item['name'] in ('MinerU CLI','Poppler','identity')):
            report['checks'].append({'name':stage,'ok':False,'skipped':True,'error':'前置检查失败，未执行'})
            continue
        print(f'Checking {stage} ...',flush=True)
        try:
            result = subprocess.run([sys.executable,'-m','skill_toolbox.web.doctor',stage],capture_output=True,
                env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},timeout=180)
            item = json.loads(result.stdout.decode('utf-8').splitlines()[-1])
            report['checks'].append({'name':stage,**item})
        except Exception as exc:
            report['checks'].append({'name':stage,'ok':False,'error':str(exc)})
    report['passed'] = all(item['ok'] for item in report['checks'])
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    for item in report['checks']:
        print(('PASS' if item['ok'] else 'FAIL') + ' ' + item['name'])
        if item['name'] == 'identity' and item['ok']:
            detail = item['detail']
            print(f"{detail['name']} {detail['version']} — {detail['application_path']}")
        if not item['ok']:
            print(item.get('error',''))
    print(f'Report: {report_path}')
    print('环境功能检查通过；云端凭据尚未验证。' if report['passed'] else '环境检查失败，请先修复，不要继续模型压测。')
    return report['passed']


if __name__ == '__main__':
    try:
        print(json.dumps({'ok':True,'detail':probe(sys.argv[1])},ensure_ascii=False))
    except Exception:
        print(json.dumps({'ok':False,'error':traceback.format_exc()},ensure_ascii=False))
        sys.exit(1)
