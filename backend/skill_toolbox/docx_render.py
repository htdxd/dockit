"""render_pages.py — render a .docx to per-page PNGs for vision verification.

Usage: python render_pages.py <input.docx> <output_dir>

Pipeline: DOCX -> PDF -> PNG.
- Windows: Word COM (ExportAsFixedFormat) — best fidelity for Word/WPS.
- Fallback: LibreOffice headless (soffice --convert-to pdf) when Word is
  unavailable.
- Rasterize with pdftoppm (Poppler) at 120 DPI.

Output: JSON {"pdf": ..., "images": [...page-N.png...], "page_images_created": N}
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

def workspace_path(value: str, must_exist: bool) -> Path:
    """Resolve a file or output directory within the current workspace."""
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def _run_capture(command: list[str], timeout: int) -> tuple[int, str, str]:
    """bytes 捕获 + UTF-8 容错解码：中文 Windows 默认 GBK，text=True 会在
    子进程输出 UTF-8（soffice/含中文文件名）时触发 UnicodeDecodeError，
    进而 stdout/stderr 变 None、后续切片抛 TypeError。"""
    completed = subprocess.run(command, capture_output=True, timeout=timeout)
    return (
        completed.returncode or 0,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def _ps_str(value: Path) -> str:
    """PowerShell 单引号字符串字面量：除 '' 外无任何转义，
    反斜杠/空格/中文/`$`/反引号全部安全。"""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_encoded(script: str) -> str:
    """PowerShell -EncodedCommand：UTF-16LE base64。json.dumps 会把中文路径
    变成反斜杠 u 开头的转义序列、反斜杠翻倍，PowerShell 不会反解，导致 Word
    打不开（中文文件名场景）。-EncodedCommand 从根上绕开命令行引号/编码问题。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _render_with_word(source: Path, pdf_path: Path) -> None:
    script = (
        "$ErrorActionPreference='Stop';"
        "$word=New-Object -ComObject Word.Application;"
        "$word.Visible=$false;$word.DisplayAlerts=0;"
        f"$doc=$word.Documents.Open({_ps_str(source)},$false,$true);"
        # 先删旧 pdf，避免 ExportAsFixedFormat 弹"是否覆盖"对话框阻塞无人值守渲染
        f"Remove-Item -Force {_ps_str(pdf_path)} -ErrorAction SilentlyContinue;"
        f"$doc.ExportAsFixedFormat({_ps_str(pdf_path)},17);"
        "$doc.Close($false);$word.Quit()"
    )
    rc, stdout, stderr = _run_capture(
        ["powershell", "-NoProfile", "-EncodedCommand", _ps_encoded(script)], 180
    )
    if rc or not pdf_path.is_file():
        raise RuntimeError(stderr[-2000:] or stdout[-2000:] or "Word rendering failed")


def _render_with_soffice(source: Path, pdf_path: Path) -> None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("Neither Word COM nor LibreOffice (soffice) is available")
    rc, stdout, stderr = _run_capture(
        [
            soffice,
            "--headless",
            "--invisible",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(pdf_path.parent),
            str(source),
        ],
        300,
    )
    if rc or not pdf_path.is_file():
        raise RuntimeError(stderr[-2000:] or stdout[-2000:] or "LibreOffice rendering failed")


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: render_pages.py <input.docx> <output_dir>", file=sys.stderr)
        sys.exit(2)
    source = workspace_path(sys.argv[1], must_exist=True)
    output_dir = workspace_path(sys.argv[2], must_exist=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{source.stem}.pdf"

    try:
        _render_with_word(source, pdf_path)
    except (RuntimeError, OSError):
        try:
            _render_with_soffice(source, pdf_path)
        except RuntimeError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            sys.exit(1)

    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        print(json.dumps({"error": "pdftoppm (Poppler) not found"}, ensure_ascii=False))
        sys.exit(1)
    rendered_rc, _, rendered_stderr = _run_capture(
        [pdftoppm, "-png", "-r", "120", str(pdf_path), str(output_dir / "page")], 180
    )
    if rendered_rc:
        print(json.dumps({"error": rendered_stderr[-2000:]}, ensure_ascii=False))
        sys.exit(1)

    images = sorted(
        str((output_dir / p.name).resolve().relative_to(Path.cwd().resolve()))
        for p in output_dir.glob("page-*.png")
    )
    print(json.dumps({
        "pdf": str(pdf_path.relative_to(Path.cwd())),
        "images": images,
        "page_images_created": len(images),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
