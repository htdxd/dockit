"""PPT 可见工具 schema（实施计划 §5.5）。

模型只传固定布局与数据参数（layout/title/bullets/asset_id/表格数据），
不传坐标/EMU/SVG/路径；vendor 脚本由 PptService 调用。
"""

from __future__ import annotations

from typing import get_args

from skill_toolbox.contracts.ppt import MAX_REPAIR_CHANGES, MAX_SLIDES, PptLayout
from skill_toolbox.llm_tools.common import function_schema

_LAYOUTS = list(get_args(PptLayout))


def _slide_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "layout": {"type": "string", "enum": _LAYOUTS},
            "title": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}, "description": "bullets/agenda 要点"},
            "body": {"type": "string", "description": "title/section/quote 副文本"},
            "asset_id": {"type": "string", "description": "图片布局的真实 asset_id（不接受路径）"},
            "columns": {"type": "array", "items": {"type": "string"}, "description": "two_column 左右栏文本"},
            "table_headers": {"type": "array", "items": {"type": "string"}},
            "table_rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            "notes": {"type": "string", "description": "演讲备注（可选）"},
        },
        "required": ["layout"],
        "additionalProperties": False,
    }


def ppt_tools() -> list[dict]:
    return [
        function_schema(
            "ppt_create_outline",
            "生成有界页面大纲（固定布局池）。topic 必填；selected_source_ids 引用真实"
            " block/asset ID（可选）。",
            {
                "topic": {"type": "string"},
                "audience": {"type": "string"},
                "page_count": {"type": "integer", "minimum": 1, "maximum": MAX_SLIDES},
                "style": {"type": "string"},
                "selected_source_ids": {"type": "array", "items": {"type": "string"}},
            },
            ["topic"],
        ),
        function_schema(
            "ppt_generate",
            "用 slides 参数生成可编辑 PPTX（python-pptx 直构，固定布局）。"
            "后端运行结构门；结构失败不会交付。",
            {
                "outline_id": {"type": "string", "description": "ppt_create_outline 返回的 outline_id"},
                "slides": {"type": "array", "items": _slide_schema(), "minItems": 1, "maxItems": MAX_SLIDES},
                "template_id": {"type": "string", "description": "预留（V1 无模板）"},
            },
            ["outline_id", "slides"],
        ),
        function_schema(
            "ppt_repair",
            "受限修改：只支持文本级修正提示（V1 布局改动需重新生成）。",
            {
                "artifact_id": {"type": "string", "description": "outline_id"},
                "issues": {"type": "array", "items": {"type": "string"}},
                "changes": {"type": "array", "items": {"type": "object"}, "maxItems": MAX_REPAIR_CHANGES},
            },
            ["artifact_id", "issues"],
        ),
    ]
