"""llm_tools 包：LLM-facing 工具 schema 与 dispatcher（实施计划 §3.1）。

- common.py: tool schema 工厂和固定错误格式
- dispatcher.py: LLM tool call → 内部 Service（阶段 8 接入 Runtime）
- resume.py / docx.py / ppt.py / pdf.py: 四个领域可见工具 schema

领域 schema 由 Runtime 按 Skill 选择；普通任务只看到领域工具 + 共享工具
（read_material / create_content_plan / ask_user_questions / task_failed）。
"""

from __future__ import annotations

from skill_toolbox.llm_tools.common import (
    error_text,
    function_schema,
    operation_text,
)

__all__ = [
    "error_text",
    "function_schema",
    "operation_text",
]
