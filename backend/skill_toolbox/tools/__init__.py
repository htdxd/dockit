"""tools 包：内部 typed Python Service 层（实施计划 §3.1/§3.2）。

- legacy.py: 旧 ToolRegistry 的兼容实现（过渡期保留，runtime 双路径运行）
- workspace.py: 文件摘要、原子文本与 JSON 写入
- process.py: 受控 subprocess、超时、进程树回收
- cache.py: 通用 hash cache、原子提交、per-key lock
- materials.py: read_material / content plan 内部 Service
- mineru.py: MinerU adapter、持久缓存、版本/参数指纹
- resume.py / docx.py / ppt.py: 三个领域 Service

旧 ``skill_toolbox.tools`` 命名空间符号（ToolRegistry/ingest_material 等）从
``tools.legacy`` 重导出，保持既有 import 与测试不回归（实施计划 §1 阶段 1）。
"""

from __future__ import annotations

import subprocess  # noqa: F401 - 兼容测试 monkeypatch "skill_toolbox.tools.subprocess.run"

from skill_toolbox.tools.legacy import (  # noqa: F401
    INGEST_DIR,
    INGEST_MANIFEST,
    MAX_DOCX_BYTES,
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    TEXT_SUFFIXES,
    ToolRegistry,
    _image_size,
    ingest_material,
    resolve_mineru_cli,
)

__all__ = [
    "INGEST_DIR",
    "INGEST_MANIFEST",
    "MAX_DOCX_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_TEXT_BYTES",
    "TEXT_SUFFIXES",
    "ToolRegistry",
    "_image_size",
    "ingest_material",
    "resolve_mineru_cli",
]
