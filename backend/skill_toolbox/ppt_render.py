"""Render a workspace PPTX to per-slide PNG files for Vision QA."""

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
    completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    return (
        completed.returncode or 0,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def ps_str(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def render_with_powerpoint(source: Path, pdf_path: Path) -> None:
    script = (
        "$ErrorActionPreference='Stop';"
        "$ppt=$null;$presentation=$null;"
        "try {"
        "$ppt=New-Object -ComObject PowerPoint.Application;"
        f"$presentation=$ppt.Presentations.Open({ps_str(source)},$true,$false,$false);"
        f"Remove-Item -Force {ps_str(pdf_path)} -ErrorAction SilentlyContinue;"
        f"$presentation.SaveAs({ps_str(pdf_path)},32);"
        "} finally {"
        "if($presentation){$presentation.Close()};"
        "if($ppt){$ppt.Quit()};"
        "}"
    )
    rc, stdout, stderr = run_capture(
        ["powershell", "-NoProfile", "-EncodedCommand", ps_encoded(script)], 240
    )
    if rc or not pdf_path.is_file():
        raise RuntimeError(stderr[-2000:] or stdout[-2000:] or "PowerPoint rendering failed")


def render_with_soffice(source: Path, pdf_path: Path) -> None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("Neither PowerPoint COM nor LibreOffice is available")
    rc, stdout, stderr = run_capture(
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
        print("Usage: ppt_render.py <input.pptx> <output_dir>", file=sys.stderr)
        raise SystemExit(2)

    source = workspace_path(sys.argv[1], must_exist=True)
    output_dir = workspace_path(sys.argv[2], must_exist=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{source.stem}.pdf"

    try:
        render_with_powerpoint(source, pdf_path)
    except (OSError, RuntimeError):
        try:
            render_with_soffice(source, pdf_path)
        except RuntimeError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(1) from None

    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        print(json.dumps({"error": "pdftoppm (Poppler) not found"}, ensure_ascii=False))
        raise SystemExit(1)

    page_prefix = f"{source.stem}-slide"
    for stale in output_dir.glob(f"{page_prefix}-*.png"):
        try:
            stale.unlink()
        except OSError:
            pass
    rendered_rc, _, rendered_stderr = run_capture(
        [pdftoppm, "-png", "-r", "120", str(pdf_path), str(output_dir / page_prefix)],
        180,
    )
    if rendered_rc:
        print(json.dumps({"error": rendered_stderr[-2000:]}, ensure_ascii=False))
        raise SystemExit(1)

    root = Path.cwd().resolve()
    images = sorted(
        str((output_dir / image.name).resolve().relative_to(root))
        for image in output_dir.glob(f"{page_prefix}-*.png")
    )
    print(
        json.dumps(
            {
                "pdf": str(pdf_path.resolve().relative_to(root)),
                "images": images,
                "page_images_created": len(images),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
