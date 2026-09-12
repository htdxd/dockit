"""contracts 包：领域 Service 的 typed 数据契约（实施计划 §3.1）。

- common.py: OperationResult / ToolError / ArtifactRef（统一内部结果）
- resume.py / docx.py / ppt.py / pdf.py: 四个领域 Service 的 request/result
"""

from __future__ import annotations

from skill_toolbox.contracts.common import (
    ArtifactRef,
    OperationResult,
    Status,
    ToolError,
)

__all__ = [
    "ArtifactRef",
    "OperationResult",
    "Status",
    "ToolError",
]
