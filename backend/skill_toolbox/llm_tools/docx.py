"""DOCX 可见工具 schema（实施计划 §5.4）。

模型不写 spec.json：docx_start 创建草稿 → docx_add_blocks 顺序追加 →
docx_finalize 自动生成/目录/机械检查 → docx_repair 只修被点名 issue。
图片只传 asset_id。
"""

from __future__ import annotations

from typing import get_args

from skill_toolbox.contracts.docx import DocxBlockType, DocxComplexity, MAX_BLOCKS_PER_CALL
from skill_toolbox.llm_tools.common import function_schema

_BLOCK_TYPES = list(get_args(DocxBlockType))


def _block_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": _BLOCK_TYPES},
            "text": {"type": "string", "description": "heading/paragraph 文本"},
            "level": {"type": "integer", "description": "heading 层级 1-6"},
            "asset_id": {"type": "string", "description": "image：真实 asset_id（不接受路径）"},
            "caption": {"type": "string"},
            "headers": {"type": "array", "items": {"type": "string"}, "description": "table 表头"},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "table 行"},
            "latex": {"type": "string", "description": "formula 的 LaTeX"},
        },
        "required": ["type"],
        "additionalProperties": False,
    }


def docx_tools() -> list[dict]:
    return [
        function_schema(
            "docx_start",
            "创建内部文档草稿（不要求模型写 spec.json）。",
            {
                "title": {"type": "string"},
                "complexity": {"type": "string", "enum": list(get_args(DocxComplexity))},
                "scene": {"type": "string", "description": "可选：设计场景"},
                "author": {"type": "string"},
                "date": {"type": "string"},
            },
            ["title"],
        ),
        function_schema(
            "docx_add_blocks",
            "按顺序追加 1-8 个内容 block（heading/paragraph/image/table/formula）。"
            "图片只传 asset_id（从 document.json 复制真实 ID）。",
            {
                "document_id": {"type": "string", "description": "docx_start 返回的 document_id"},
                "blocks": {"type": "array", "items": _block_schema(), "minItems": 1, "maxItems": MAX_BLOCKS_PER_CALL},
            },
            ["document_id", "blocks"],
        ),
        function_schema(
            "docx_finalize",
            "自动生成 DOCX、按需注入目录、运行机械质量门并注册产物。"
            "机械门失败会返回具体检查项；用 docx_repair 修复后重新 finalize。",
            {
                "document_id": {"type": "string"},
                "output_name": {"type": "string", "description": "可选：输出文件名（如 报告.docx）"},
            },
            ["document_id"],
        ),
        function_schema(
            "docx_repair",
            "只修复质量报告点名的 issue。fix 是结构化修复参数"
            "（{type: replace_block_text|remove_block, block_index, text?, output_name?}）。",
            {
                "artifact_id": {"type": "string", "description": "document_id（docx_start 返回）"},
                "issue_id": {"type": "string", "description": "质量报告点名的 issue"},
                "fix": {"type": "object", "description": "结构化修复参数"},
            },
            ["artifact_id", "issue_id", "fix"],
        ),
    ]
