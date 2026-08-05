from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def workspace_path(value: str) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(value)
    return path


def run_text(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def main() -> None:
    source = workspace_path(sys.argv[1])
    fonts = run_text(["pdffonts", str(source)])
    extracted = run_text(["pdftotext", "-f", "1", "-l", "1", str(source), "-"])
    font_rows = [line for line in fonts.splitlines()[2:] if line.strip()]
    text_chars = len(extracted.strip())

    if not font_rows and text_chars < 40:
        classification = "scanned"
    elif font_rows and text_chars >= 100:
        classification = "native_structured"
    else:
        classification = "mixed_or_uncertain"

    print(json.dumps({
        "source": str(source.relative_to(Path.cwd())),
        "classification": classification,
        "font_rows": len(font_rows),
        "first_page_text_characters": text_chars,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
