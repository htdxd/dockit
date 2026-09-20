from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shutil
import sys
import time

from .state import Store


def main():
    parser = argparse.ArgumentParser(description='DocKit 邀请试用网站')
    parser.add_argument('--config', default='server.json')
    parser.add_argument('--invite', type=int, metavar='COUNT')
    parser.add_argument('--quota', type=int, default=5)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--benchmark', action='store_true', help='仅本机：无需邀请码、不限制次数')
    parser.add_argument('--concurrency', type=int)
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if not config_path.is_file() and not args.check:
        raise SystemExit('请先复制 server.example.json 为 server.json，并填写服务端配置。')
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.is_file() else {}
    config['data_dir'] = str((config_path.parent / config.get('data_dir', 'data')).resolve())
    if args.port is not None:
        config['port'] = args.port
    if args.concurrency is not None:
        config['max_concurrent_tasks'] = args.concurrency
    if args.benchmark:
        config.update(benchmark_mode=True, secure_cookie=False,
                      public_origin=f"http://127.0.0.1:{config.get('port', 8080)}")
    if args.check:
        from .doctor import run_check
        raise SystemExit(0 if run_check(config_path.parent/'environment-check.json',config) else 1)
    if args.invite is not None:
        if not 1 <= args.invite <= 100 or not 1 <= args.quota <= 100:
            raise SystemExit('每次生成 1–100 个邀请码，每个限 1–100 次')
        store = Store(Path(config['data_dir']))
        for _ in range(args.invite):
            print(store.invite(args.quota))
        return
    from skill_toolbox.models import ProviderConfig
    try:
        provider = ProviderConfig.model_validate(config.get('provider', {}))
    except ValueError:
        raise SystemExit('provider 配置格式不正确，请对照 server.example.json；未输出配置内容以保护密钥。') from None
    if not provider.api_key or provider.api_key.startswith('REPLACE_') or not config.get('mineru_key') or config['mineru_key'].startswith('REPLACE_'):
        raise SystemExit('请在 server.json 中填写 LLM 和 MinerU 密钥；不要将该文件放入网站静态目录。')
    if provider.vision is not True or provider.tool_calling is not True:
        raise SystemExit('本网站需要具备 vision 和 tool_calling 的模型；请先使用 --probe 验证。')
    if args.probe:
        from skill_toolbox.providers import create_provider
        from skill_toolbox.models import CapabilityProbeReport, ProbeResult
        result = asyncio.run(create_provider(provider).probe(['tool_calling', 'vision', 'forced_tool_calling']))
        report = CapabilityProbeReport()
        for name, (status, _) in result.items():
            setattr(report, name, ProbeResult(status=status, checked_at=time.time()))
            print(f'{name}: {status}')
        config['provider']['capability_probe'] = report.model_dump_json()
        # 配置文件只在管理员显式探测时更新；不输出凭据。
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        if result.get('tool_calling', ('unknown',))[0] != 'verified' or result.get('vision', ('unknown',))[0] != 'verified':
            raise SystemExit('工具或视觉能力未验证通过，请换用可靠模型后再开放网站。')
        return
    if sys.platform != 'win32':
        raise SystemExit('当前简历渲染需要 Windows + Microsoft Word；此版本不支持 Linux 渲染。')
    from skill_toolbox.tools import resolve_mineru_cli
    resolve_mineru_cli()
    if not shutil.which('pdftoppm'):
        raise SystemExit('缺少随包提供的 Poppler，请使用 start-web.bat 启动。')
    from skill_toolbox.word_com import start_word, close_word
    word = start_word()
    close_word(word)
    if config.get('secure_cookie', True) and not config.get('public_origin', '').startswith('https://'):
        raise SystemExit('正式部署需要 HTTPS public_origin；仅本机测试可设置 secure_cookie=false。')
    from .app import create_app
    import uvicorn
    log_dir = Path(config['data_dir']) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'server.log', maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    # 单 API 进程内调度独立生成进程；不启用 reload 或多个 Uvicorn workers。
    uvicorn.run(create_app(config), host='127.0.0.1', port=int(config.get('port', 8080)),
                proxy_headers=True, forwarded_allow_ips='127.0.0.1', access_log=False)


if __name__ == '__main__':
    main()
