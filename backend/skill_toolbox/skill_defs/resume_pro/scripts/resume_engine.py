# -*- coding: utf-8 -*-
"""resume_engine.py — resume_pro skill 脚本共享工具（自包含，不依赖 docx_pro）"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def workspace_path(value: str, must_exist: bool) -> Path:
    """以 cwd（workspace 根）为基准解析路径，拒绝逃逸"""
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def prepend_script_dir() -> None:
    """把脚本所在目录加入 sys.path（供同目录模块导入）"""
    sys.path.insert(0, str(Path(__file__).parent))
