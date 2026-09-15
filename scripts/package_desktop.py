"""准备 Windows 安装包的离线运行资源：uv run scripts/package_desktop.py。"""
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "backend-dist/desktop"


def main():
    if sys.platform != "win32" or Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise SystemExit("请在 Windows 项目环境中运行：uv run scripts/package_desktop.py")
    # 只清理本脚本的构建产物，不碰项目源码、用户配置或产物。
    target = OUTPUT.resolve()
    target.relative_to((ROOT / "backend-dist").resolve())
    if OUTPUT.is_symlink():
        raise SystemExit("构建目录不能是符号链接")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".env", ".env.*", "*.log", "_editable*", "_virtualenv*")
    python = target / "python"
    shutil.copytree(sys.base_prefix, python, ignore=ignore)
    packages = python / "Lib/site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / ".venv/Lib/site-packages", packages, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_virtualenv*", "_skill_toolbox*", "_editable*",
                                                 "pytest*", "_pytest", "ruff*", ".env", ".env.*"))
    from skill_toolbox.resume_layout.profiles import SUPPORTED_TEMPLATES
    def source_ignore(directory, names):
        excluded = set(ignore(directory, names))
        path = Path(directory)
        if path.name == "templates" and path.parent.name == "resume_pro":
            excluded.update(name for name in names if name not in SUPPORTED_TEMPLATES)
        return excluded
    shutil.copytree(ROOT / "backend/skill_toolbox", target / "backend/skill_toolbox", ignore=source_ignore)

    from skill_toolbox.tools import resolve_mineru_cli
    cli = resolve_mineru_cli()
    if len(cli) == 2 and Path(cli[1]).name == "mineru-open-api":
        npm_package = Path(cli[1]).parent.parent
        binary = npm_package / "node_modules/mineru-open-api-win32-x64/bin/mineru-open-api.exe"
    else:
        binary = Path(cli[0])
    if not binary.is_file() or binary.suffix.lower() != ".exe":
        raise SystemExit("未找到可随安装包分发的 MinerU Windows 原生程序")
    (target / "tools").mkdir()
    shutil.copy2(binary, target / "tools/mineru-open-api.exe")
    poppler = shutil.which("pdftoppm")
    if not poppler:
        raise SystemExit("构建机缺少 Poppler")
    shutil.copytree(Path(poppler).parent.parent, target / "poppler", ignore=ignore)
    # 同时保留已安装发行物的许可信息。
    licenses = Path(poppler).parent.parent.parent / "info/licenses"
    if licenses.is_dir():
        shutil.copytree(licenses, target / "poppler/licenses")
    for name in ("LICENSE", "LICENSE.txt", "package.json"):
        source = binary.parent.parent / name
        if source.is_file():
            shutil.copy2(source, target / "tools" / name)
    (target / "python-packages.json").write_text(json.dumps(
        sorted({(d.metadata["Name"], d.version) for d in importlib.metadata.distributions()}),
        ensure_ascii=False, indent=2), encoding="utf-8")
    env = {**os.environ, "PYTHONHOME": str(python), "PYTHONPATH": str(target / "backend"),
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("__PYVENV_LAUNCHER__", None)
    subprocess.run([str(python / "python.exe"), "-c",
                    "import win32com.client, pymupdf, pptx, docx, openai, anthropic; print('Bundled Python OK')"],
                   cwd=target, env=env, check=True, timeout=120)
    print(f"运行资源已准备：{target}")


if __name__ == "__main__":
    main()
