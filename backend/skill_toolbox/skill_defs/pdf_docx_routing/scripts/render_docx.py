from __future__ import annotations

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


def main() -> None:
    source = workspace_path(sys.argv[1], must_exist=True)
    output_dir = workspace_path(sys.argv[2], must_exist=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{source.stem}.pdf"

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

    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        rendered = subprocess.run(
            [pdftoppm, "-png", "-r", "120", str(pdf_path), str(output_dir / "page")],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if rendered.returncode:
            raise RuntimeError(rendered.stderr[-2000:])

    images = sorted(path.name for path in output_dir.glob("page-*.png"))
    print(json.dumps({
        "pdf": str(pdf_path.relative_to(Path.cwd())),
        "images": images,
        "page_images_created": len(images),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
