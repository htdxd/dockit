"""PPT 领域 typed contracts（实施计划 §5.5）。

V1 不让模型自由编辑 SVG：只允许固定布局、文本、图片和表格参数。vendor
脚本由 PptService 调用，模型不接触上游 CLI 参数。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PptLayout = Literal[
    "title", "section", "bullets", "two_column", "image_text", "image_full",
    "table", "agenda", "quote", "end",
]
MAX_SLIDES = 30
MAX_REPAIR_CHANGES = 8


class SlideSpec(BaseModel):
    """单页幻灯片的有界参数。layout 是固定布局；文本/图片/表格数据化。

    layout 是 Pydantic 字面量：非法布局在 typed 参数校验层直接被拒
    （ValidationError），不会到达 Service。
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    layout: PptLayout = "bullets"
    title: str = ""
    bullets: list[str] = Field(default_factory=list)
    body: str = ""
    asset_id: str = ""
    columns: list[str] = Field(default_factory=list)
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    notes: str = ""


class DeckOutline(BaseModel):
    topic: str
    audience: str = ""
    page_count: int = Field(default=8, ge=1, le=MAX_SLIDES)
    style: str = "clean"
    selected_source_ids: list[str] = Field(default_factory=list)
    slides: list[SlideSpec] = Field(default_factory=list)


class PptGenerateRequest(BaseModel):
    outline_id: str
    slides: list[SlideSpec] = Field(default_factory=list, max_length=MAX_SLIDES)
    template_id: str = ""


class PptRepairRequest(BaseModel):
    artifact_id: str
    issues: list[str] = Field(default_factory=list)
    # 有限文本/布局修改（模型不传坐标/EMU/SVG）
    changes: list[dict] = Field(default_factory=list, max_length=MAX_REPAIR_CHANGES)
