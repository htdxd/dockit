from __future__ import annotations

import json
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


def boolean(value: str) -> str:
    normalized = value.lower()
    if normalized not in {"true", "false"}:
        raise ValueError("Boolean values must be true or false")
    return normalized


def main() -> None:
    source = workspace_path(sys.argv[1], must_exist=True)
    output = workspace_path(sys.argv[2], must_exist=False)
    model, ocr, formula, table, language, pages = sys.argv[3:]
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "mineru-open-api", "extract", str(source), "-o", str(output.parent), "-f", "docx",
        "--model", model, "--ocr=" + boolean(ocr), "--formula=" + boolean(formula),
        "--table=" + boolean(table), "--language", language, "--timeout", "1800",
    ]
    if pages.lower() != "all":
        command.extend(["--pages", pages])

    completed = subprocess.run(command, capture_output=True, text=True, timeout=1860)
    docx_files = sorted(output.parent.glob("*.docx"))
    if completed.returncode or not docx_files:
        raise RuntimeError(completed.stderr[-2000:] or completed.stdout[-2000:] or "MinerU did not create a DOCX")

    generated = docx_files[0]
    if generated.resolve() != output.resolve():
        generated.replace(output)
    print(json.dumps({"output": str(output.relative_to(Path.cwd())), "model": model}, ensure_ascii=False))


if __name__ == "__main__":
    main()
