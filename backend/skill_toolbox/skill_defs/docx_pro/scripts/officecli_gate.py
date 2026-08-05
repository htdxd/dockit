"""officecli_gate.py — detect whether the optional OfficeCLI binary is available.

Usage: python officecli_gate.py [KEY=VALUE ...]

OfficeCLI (Apache 2.0, https://github.com/iOfficeAI/OfficeCLI) is an optional
enhancement engine for native charts / watermarks / mail-merge / comments.
When present, the docx_pro prompt may delegate complex scenarios to it via
``exec_cmd args.env`` (the env pairs are appended to the command line as
KEY=VALUE and read here with os.environ).

Output: JSON {"available": bool, "version": str|null, "hint": str}

Never claims availability without a successful `--version` probe.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def _probe(binary: str) -> str | None:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else "unknown"


def main() -> None:
    # CLI env overrides come as KEY=VALUE tokens after the script path
    # (plumbed through exec_cmd args.env). Apply them so later delegated
    # invocations inherit the same environment.
    for token in sys.argv[1:]:
        if "=" in token:
            key, _, value = token.partition("=")
            os.environ[key] = value

    configured = os.environ.get("DOCX_PRO_CLI", "").strip()
    candidates = [configured] if configured else []
    candidates += ["officecli"]
    for binary in candidates:
        if not binary:
            continue
        path = shutil.which(binary)
        if path is None and os.path.sep in binary and os.path.exists(binary):
            path = binary
        if path is None:
            continue
        version = _probe(path)
        if version is not None:
            print(json.dumps({
                "available": True,
                "binary": path,
                "version": version,
            }, ensure_ascii=False))
            return
    print(json.dumps({
        "available": False,
        "version": None,
        "hint": (
            "OfficeCLI not found. Install via "
            "https://github.com/iOfficeAI/OfficeCLI (Apache 2.0) or set "
            "DOCX_PRO_CLI to the binary path. Without it, docx_pro uses the "
            "pure python-docx route (no native charts/watermarks/mail-merge)."
        ),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
