"""材料补查、问答和任务结束工具；在 ResumeTask 中与 handler 绑定。"""

from __future__ import annotations

from typing import Any



def function_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """共享控制工具的 schema（JSON Schema function）。"""
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






def shared_tools() -> list[dict]:
    return [
        function_schema(
            "read_material",
            "首次消息已提供准备后的材料；需要补查时用 material_id 配合 view=blocks 读取正文（默认200块）；"
            "has_more=true 时用 next_offset 继续。也可使用返回的真实 block/asset ID 单独读取。"
            "材料ID配合 summary 查看摘要、assets 查看资源列表。"
            "单独读取图片 asset 时有 Vision 返回实际图片，无 Vision 仅返回元数据。不接受任意路径。",
            {
                "source_id": {"type": "string", "description": "材料 material_id，或 IR block/asset 的真实 ID"},
                "view": {"type": "string", "enum": ["summary", "blocks", "assets", "native_text"], "description": "默认 summary；PDF/DOCX 可选 native_text 补查原件文字：PDF 的 offset/limit 按页，DOCX 按内容块计数，不重复调用主解析。"},
                "offset": {"type": "integer", "minimum": 0, "description": "材料ID：块/资源列表偏移；单block ID：字符偏移"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "材料ID：块/资源数量；单block ID：字符数（默认200）"},
            },
            ["source_id"],
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
