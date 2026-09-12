"""DOCX 领域 typed contracts（实施计划 §5.4）。

模型只提交有序 blocks；图片只传 ``asset_id``（后端解析为真实文件路径）；
模型不写 ``work/spec.json``。``docx_repair`` 只修复质量报告点名的 issue。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

DocxBlockType = Literal["heading", "paragraph", "image", "table", "formula"]


class DocxBlock(BaseModel):
    type: DocxBlockType
    text: str = ""
    level: int = 1
    # 图片只接受 asset_id（后端通过 IR/catalog 解析真实路径）
    asset_id: str = ""
    caption: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    latex: str = ""

    @field_validator("level")
    @classmethod
    def _v_level(cls, value: int) -> int:
        if not 1 <= value <= 6:
            raise ValueError("heading level 必须在 1-6")
        return value


class DocxStartRequest(BaseModel):
    title: str
    complexity: Literal["simple", "standard", "academic", "gongwen", "form", "template"] = "standard"
    scene: str = ""
    author: str = ""
    date: str = ""


class DocxAddBlocksRequest(BaseModel):
    document_id: str
    blocks: list[DocxBlock] = Field(default_factory=list, max_length=8)


class DocxFinalizeRequest(BaseModel):
    document_id: str
    output_name: str = ""


class DocxRepairRequest(BaseModel):
    artifact_id: str
    issue_id: str
    # 结构化修复参数（有限字段，后端映射为 spec 修正）
    fix: dict = Field(default_factory=dict)
