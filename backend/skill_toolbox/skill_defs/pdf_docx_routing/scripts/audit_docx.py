from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path


def workspace_path(value: str) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(value)
    return path


def main() -> None:
    source = workspace_path(sys.argv[1])
    with zipfile.ZipFile(source) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        media_files = [name for name in archive.namelist() if name.startswith("word/media/")]

    text = re.sub(r"<[^>]+>", " ", xml)
    latex = len(re.findall(r"\$\$|\\(?:begin|frac|Psi|varPsi)", text))
    layout_tokens = len(re.findall(r"<\|(?:box|ref)_(?:start|end)\|>", text))
    print(json.dumps({
        "source": str(source.relative_to(Path.cwd())),
        "latex_markers": latex,
        "layout_tokens": layout_tokens,
        "embedded_media": len(media_files),
        "visual_verification_required": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
