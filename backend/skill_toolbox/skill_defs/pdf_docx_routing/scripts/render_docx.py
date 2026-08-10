from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path


def workspace_path(value: str, must_exist: bool) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def run_capture(command: list[str], timeout: int) -> tuple[int, str, str]:
    # bytes 捕获 + UTF-8 容错解码：中文 Windows 默认 GBK，text=True 会在
    # 子进程输出 UTF-8（含中文文件名）时触发 UnicodeDecodeError。
    completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    return (
        completed.returncode or 0,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def ps_str(value: Path) -> str:
    """PowerShell 单引号字符串字面量：除 '' 外无任何转义，
    反斜杠/空格/中文/`$`/反引号全部安全（单引号串不插值、不解释转义）。"""
    return "'" + str(value).replace("'", "''") + "'"


def ps_encoded(script: str) -> str:
    """PowerShell -EncodedCommand：UTF-16LE base64，彻底绕开命令行引号与
    JSON 转义问题（json.dumps 会把中文变成反斜杠 u 开头的转义序列、反斜杠
    翻倍，PowerShell 不会反解，导致 Word 打不开中文路径的文件）。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def main() -> None:
    source = workspace_path(sys.argv[1], must_exist=True)
    # 模型偶尔会漏传 output_dir（tools.py 的占位符检查会先拦一次），这里再
    # 兜底一次：缺省时渲染到源文件所在目录（artifacts/），避免写进名为
    # "{output_dir}" 的目录导致后续 read 全部找不到图片。
    if len(sys.argv) < 3 or "{" in sys.argv[2]:
        output_dir = source.parent
    else:
        output_dir = workspace_path(sys.argv[2], must_exist=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{source.stem}.pdf"
    # 每个文档使用独立 PNG 前缀（<stem>-page-），渲染前清理该文档自身旧
    # 输出，禁止跨文档残留（实施计划 §10.3）。并发渲染多个 DOCX 时各自的
    # page-*.png 前缀不再互相覆盖。
    page_prefix = f"{source.stem}-page"
    for stale in output_dir.glob(f"{page_prefix}-*.png"):
        try:
            stale.unlink()
        except OSError:
            pass

    script = (
        "$ErrorActionPreference='Stop';"
        "$word=New-Object -ComObject Word.Application;"
        "$word.Visible=$false;$word.DisplayAlerts=0;"
        f"$doc=$word.Documents.Open({ps_str(source)},$false,$true);"
        # 先删旧 pdf，避免 ExportAsFixedFormat 弹"是否覆盖"对话框阻塞无人值守渲染
        f"Remove-Item -Force {ps_str(pdf_path)} -ErrorAction SilentlyContinue;"
        f"$doc.ExportAsFixedFormat({ps_str(pdf_path)},17);"
        "$doc.Close($false);$word.Quit()"
    )
    rc, stdout, stderr = run_capture(
        ["powershell", "-NoProfile", "-EncodedCommand", ps_encoded(script)], 180
    )
    if rc or not pdf_path.is_file():
        raise RuntimeError(stderr[-2000:] or stdout[-2000:] or "Word rendering failed")

    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        rendered_rc, _, rendered_stderr = run_capture(
            [pdftoppm, "-png", "-r", "120", str(pdf_path), str(output_dir / page_prefix)], 180
        )
        if rendered_rc:
            raise RuntimeError(rendered_stderr[-2000:])

    cwd = Path.cwd().resolve()
    # 图片输出为工作区相对路径（如 artifacts/report-page-1.png），模型可直接 read。
    images = sorted(
        str((output_dir / p.name).resolve().relative_to(cwd))
        for p in output_dir.glob(f"{page_prefix}-*.png")
    )
    print(json.dumps({
        "pdf": str(pdf_path.resolve().relative_to(cwd)),
        "images": images,
        "page_images_created": len(images),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
