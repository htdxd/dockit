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

import json
import shutil
import subprocess
import sys
from pathlib import Path

from docx_pro_engine import workspace_path


def _render_with_word(source: Path, pdf_path: Path) -> None:
    source_json = json.dumps(str(source))
    pdf_json = json.dumps(str(pdf_path))
    command = (
        "$ErrorActionPreference='Stop';"
        "$word=New-Object -ComObject Word.Application;"
        "$word.Visible=$false;$word.DisplayAlerts=0;"
        f"$doc=$word.Documents.Open({source_json},$false,$true);"
        f"$doc.ExportAsFixedFormat({pdf_json},17);"
        "$doc.Close();$word.Quit()"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode or not pdf_path.is_file():
        raise RuntimeError(completed.stderr[-2000:] or "Word rendering failed")


def _render_with_soffice(source: Path, pdf_path: Path) -> None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("Neither Word COM nor LibreOffice (soffice) is available")
    completed = subprocess.run(
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
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode or not pdf_path.is_file():
        raise RuntimeError(completed.stderr[-2000:] or "LibreOffice rendering failed")


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
    rendered = subprocess.run(
        [pdftoppm, "-png", "-r", "120", str(pdf_path), str(output_dir / "page")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if rendered.returncode:
        print(json.dumps({"error": rendered.stderr[-2000:]}, ensure_ascii=False))
        sys.exit(1)

    images = sorted(p.name for p in output_dir.glob("page-*.png"))
    print(json.dumps({
        "pdf": str(pdf_path.relative_to(Path.cwd())),
        "images": images,
        "page_images_created": len(images),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
