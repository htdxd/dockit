"""共享可见工具 schema、schema 工厂与固定错误格式（实施计划 §3.2/§5.2）。

四个 Skill 只共享这些工具；普通任务不暴露 write/edit/exec_cmd/spec_append。
finish_task 由 Runtime 自动执行；如保留为 LLM 兜底，只接受已注册 artifact_id[]。
"""

from __future__ import annotations

import json
from typing import Any

from skill_toolbox.contracts.common import OperationResult, ToolError


def function_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """与旧 tool_specs._function 相同的 schema 形状（JSON Schema function）。"""
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def operation_text(result: OperationResult) -> str:
    """把内部 OperationResult 翻译为 LLM 可见的紧凑文本（不含 traceback）。"""
    return json.dumps(result.as_dict(), ensure_ascii=False, indent=2)


def error_text(exc: ToolError) -> str:
    """固定错误格式：code / message / retryable / suggestion。"""
    return json.dumps(exc.to_dict(), ensure_ascii=False)


def shared_tools() -> list[dict]:
    return [
        function_schema(
            "read_material",
            "按 IR source_id 读取材料摘要、正文分页或 Asset 元数据。"
            "source_id 必须从 document.json 的 blocks[].id / assets[].id 原样复制"
            "（block-<材料id16>-<序号> / asset-<材料id16>-<hash>），不接受任意路径。"
            "无 Vision 时图片只返回元数据（文件名/尺寸/aspect/bbox/caption），不返回图片字节。",
            {
                "source_id": {"type": "string", "description": "IR block 或 asset 的真实 ID"},
                "view": {"type": "string", "enum": ["summary", "blocks", "assets"], "description": "读取视图（默认 summary）"},
                "offset": {"type": "integer", "minimum": 0, "description": "正文分页偏移"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "正文分页行数"},
            },
            ["source_id"],
        ),
        function_schema(
            "create_content_plan",
            "由后端写入 work/plans/content-plan.json 并补齐 task_type/mode/schema_version。"
            "selected_source_ids / excluded_source_ids 必须是真实 source_id；"
            "每个候选 Asset 必须被选择或明确排除（未列出的 Asset 自动排除并留痕）。",
            {
                "selected_source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要使用的 block/asset 真实 ID 列表",
                },
                "excluded_source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要排除的 block/asset 真实 ID 列表（可选）",
                },
                "exclusion_reasons": {
                    "type": "object",
                    "description": "可选：source_id → 排除原因（irrelevant/duplicate/low_confidence/unsupported/user_rejected）",
                },
            },
            ["selected_source_ids"],
        ),
        function_schema(
            "ask_user_questions",
            "暂停任务并向用户提出结构化问题（唯一获取用户输入的通道）。"
            "一次问清全部问题，任务在用户提交答案后继续。",
            {
                "questions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "全部问题：{id, label, type: text|textarea|select, required?, options?}",
                }
            },
            ["questions"],
        ),
        function_schema(
            "task_failed",
            "以明确原因终止任务。外部步骤失败且无法恢复时必须调用，"
            "禁止用 finish_task 伪造 artifact。必须是本轮唯一工具调用。",
            {"error": {"type": "string", "description": "用户可读的失败原因"}},
            ["error"],
        ),
        function_schema(
            "finish_task",
            "完成任务（由 Runtime 自动执行交付）。只接受已注册 artifact 的相对路径，"
            "不接受任意路径。必须是本轮唯一工具调用。",
            {
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "已注册 artifact 的 workspace 相对路径",
                }
            },
            ["artifacts"],
        ),
    ]
