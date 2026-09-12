"""受控路径与原子文件操作（实施计划 §3.2 内部 tools 层）。

路径策略复用 ``policy.WorkspacePolicy``（拒绝绝对路径/越界）；本模块补充
Service 层常用的原子写与文本读取助手，避免各领域 Service 各自实现。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.tools.legacy import MAX_TEXT_BYTES, _image_size


def policy_for(workspace: Path, read_roots: tuple[Path, ...] = ()) -> WorkspacePolicy:
    return WorkspacePolicy(workspace, read_roots=read_roots)


def atomic_write_text(path: Path, content: str) -> None:
    """temp + os.replace 原子写 UTF-8 文本（并发读时不暴露半写文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_text_lines(path: Path, offset: int = 0, limit: int = 500) -> str:
    """读取文本文件的分页视图；超 1MB 直接拒绝（对齐 legacy read 契约）。"""
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("Text file exceeds the 1 MB read limit")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[offset : offset + limit])


def image_metadata(path: Path) -> dict[str, Any]:
    """图片尺寸元数据（复用 legacy 的纯标准库 JPEG/PNG 解析）。"""
    dims = _image_size(path)
    width, height = dims or (None, None)
    return {
        "width": width,
        "height": height,
        "aspect": round(width / height, 2) if width and height else None,
        "size": path.stat().st_size,
    }
