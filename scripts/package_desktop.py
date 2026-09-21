"""准备 Windows 安装包的离线运行资源：uv run scripts/package_desktop.py。"""
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "backend-dist/desktop"


def poppler_runtime_files(binary_dir: Path) -> list[Path]:
    """保留两个 PDF 工具的传递 DLL 依赖；objdump 仅构建机需要。"""
    objdump = shutil.which("objdump")
    if not objdump:
        raise SystemExit("裁剪 Poppler 需要构建机安装 objdump（MinGW/binutils），用户无需安装")
    available = {p.name.lower(): p for p in binary_dir.iterdir() if p.is_file()}
    pending = [available[name] for name in ("pdftoppm.exe", "pdfinfo.exe")]
    selected = {}
    while pending:
        binary = pending.pop()
        if binary.name.lower() in selected:
            continue
        selected[binary.name.lower()] = binary
        imports = subprocess.run([objdump, "-p", str(binary)], capture_output=True,
                                 text=True, check=True, timeout=30).stdout
        for name in re.findall(r"DLL Name:\s*(\S+)", imports):
            dependency = available.get(name.lower())
            if dependency is not None:
                pending.append(dependency)
            elif not name.lower().startswith(("api-ms-", "ext-ms-")) and not (
                Path(os.environ["SystemRoot"]) / "System32" / name
            ).is_file():
                raise SystemExit(f"Poppler 缺少依赖：{binary.name} → {name}")
    return sorted(selected.values())


def main(*, resume_web=False):
    if sys.platform != "win32" or Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise SystemExit("请在 Windows 项目环境中运行：uv run scripts/package_desktop.py")
    # 只清理本脚本的构建产物，不碰项目源码、用户配置或产物。
    output = ROOT / 'backend-dist/resume-web/runtime' if resume_web else OUTPUT
    target = output.resolve()
    target.relative_to((ROOT / "backend-dist").resolve())
    if output.is_symlink():
        raise SystemExit("构建目录不能是符号链接")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".env", ".env.*", "*.log", "_editable*", "_virtualenv*")
    python = target / "python"
    def python_ignore(directory, names):
        excluded = set(ignore(directory, names))
        relative = Path(directory).relative_to(sys.base_prefix).as_posix()
        unused = {
            ".": {"include", "libs", "tcl"},
            "Lib": {"ensurepip", "idlelib", "tkinter", "turtledemo", "test"},
            "Lib/site-packages": {"pip"},
            "DLLs": {"_tkinter.pyd", "tcl86t.dll", "tk86t.dll"},
        }
        excluded.update(set(names) & unused.get(relative, set()))
        if relative == "Lib/site-packages":
            excluded.update(name for name in names if name.startswith("pip-"))
        return excluded
    shutil.copytree(sys.base_prefix, python, ignore=python_ignore)
    packages = python / "Lib/site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / ".venv/Lib/site-packages", packages, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_virtualenv*", "_skill_toolbox*", "_editable*",
                                                 "pytest*", "_pytest", "ruff*", "pip", "pip-*.dist-info",
                                                 "pygments", "pygments-*.dist-info", "iniconfig*",
                                                 "tests", "test", "demos", ".env", ".env.*"))
    from skill_toolbox.resume_layout.profiles import SUPPORTED_TEMPLATES
    def source_ignore(directory, names):
        excluded = set(ignore(directory, names))
        path = Path(directory)
        if path.name == 'skill_defs':
            excluded.update(name for name in names if name != 'resume_pro')
        if path.name == "templates" and path.parent.name == "resume_pro":
            excluded.update(name for name in names if name not in SUPPORTED_TEMPLATES | {'LICENSE'})
        return excluded
    shutil.copytree(ROOT / "backend/skill_toolbox", target / "backend/skill_toolbox", ignore=source_ignore)

    for notice in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / notice, target / notice)
    if not (ROOT / 'licenses/sources.json').is_file():
        raise SystemExit('先运行 uv run scripts/release_sources.py，准备第三方许可与对应源码清单')
    shutil.copytree(ROOT / 'licenses', target / 'licenses')

    from skill_toolbox.tools.mineru import resolve_mineru_cli
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
    poppler_source = Path(poppler).parent
    poppler_target = target / "poppler"
    (poppler_target / "bin").mkdir(parents=True)
    for source in poppler_runtime_files(poppler_source):
        shutil.copy2(source, poppler_target / "bin" / source.name)
    # 字符映射和字体配置是运行数据，不能按 DLL 引用裁掉。
    for name in ("etc", "share"):
        if (poppler_source.parent / name).is_dir():
            shutil.copytree(poppler_source.parent / name, poppler_target / name, ignore=ignore)
    # 同时保留已安装发行物的许可信息。
    licenses = Path(poppler).parent.parent.parent / "info/licenses"
    if licenses.is_dir():
        shutil.copytree(licenses, target / "poppler/licenses")
    for name in ("LICENSE", "LICENSE.txt", "package.json"):
        source = binary.parent.parent / name
        if source.is_file():
            shutil.copy2(source, target / "tools" / name)
    (target / "python-packages.json").write_text(json.dumps(
        sorted({(d.metadata["Name"], d.version) for d in importlib.metadata.distributions(path=[str(packages)])}),
        ensure_ascii=False, indent=2), encoding="utf-8")
    env = {**os.environ, "PYTHONHOME": str(python), "PYTHONPATH": str(target / "backend"),
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("__PYVENV_LAUNCHER__", None)
    subprocess.run([str(python / "python.exe"), "-c",
                    "import win32com.client, pymupdf, docx, openai, anthropic; print('Bundled Python OK')"],
                   cwd=target, env=env, check=True, timeout=120)
    print(f"运行资源已准备：{target}")


if __name__ == "__main__":
    main(resume_web='--resume-web' in sys.argv)
