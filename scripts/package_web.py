"""uv run --extra web scripts/package_web.py：构建无密钥的 Windows 网站部署包。"""
from pathlib import Path
import shutil
import subprocess
import zipfile

from package_desktop import main

ROOT = Path(__file__).resolve().parents[1]

def write_archive():
    bundle = ROOT / 'backend-dist/resume-web'
    # 明确白名单，绝不打包本机 server.json、SQLite、上传材料或任务日志。
    public_files = ['start-web.bat', 'start-benchmark.bat', 'stress-test.bat', 'server.example.json', 'Caddyfile.example', 'DEPLOY.md', 'LOADTEST.md']
    for name in public_files:
        shutil.copy2(ROOT / 'deploy/resume-web' / name, bundle / name)
    output = ROOT / 'backend-dist/DocKit-Resume-Web-Windows.zip'
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        paths = [bundle / name for name in public_files]
        paths.extend(path for path in (bundle / 'runtime').rglob('*') if path.is_file())
        for path in paths:
            archive.write(path, Path('DocKit-Resume-Web') / path.relative_to(bundle))
    print(f'Website package: {output} ({output.stat().st_size/1024/1024:.1f} MiB)')


if __name__ == '__main__':
    subprocess.run(['node', str(ROOT / 'scripts/build_web.mjs')], cwd=ROOT, check=True)
    main(resume_web=True)
    write_archive()
