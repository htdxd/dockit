"""共享材料理解的最小数据契约（实施计划 §8）。

四个功能（PPT / 简历 / DOCX / PDF 转 DOCX）共用同一份 DocumentIR、
AssetCatalog、ContentPlan 与 QAReport。只保留当前工作流真正消费的字段，
额外原生事实（EMU 坐标、复杂表格结构等）放各材料的 native/source-map.json，
不扩张 Block/Asset schema（实施计划 §8.1 约束）。

路径约定：所有暴露给 Agent 的路径必须是 workspace 相对路径；本模块只负责
schema 与校验，不接触文件系统。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

BlockType = Literal[
    "heading", "paragraph", "list", "table", "formula", "code", "image", "page_break"
]
SourceFormat = Literal["pdf", "docx", "md", "txt", "pptx", "image"]
TaskType = Literal["ppt", "resume", "docx", "pdf_to_docx"]
PlanningMode = Literal["vision", "conservative"]
TransformKind = Literal["preserve", "summarize", "crop", "table", "formula"]
ExclusionReason = Literal[
    "irrelevant", "duplicate", "low_confidence", "unsupported", "user_rejected"
]
QAStatus = Literal["passed", "failed", "not_run"]
MechanicalStatus = Literal["passed", "failed"]

SCHEMA_VERSION = "1"


def _normalized_bbox(value: Any) -> Any:
    """bbox 必须是 4 元素数字列表（页面归一化坐标 [0,1]）或 None。"""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("bbox must be [x, y, width, height] or null")
    result = [float(v) for v in value]
    if not all(0.0 <= v <= 1.0 for v in result):
        raise ValueError("bbox uses page-normalized coordinates in [0, 1]")
    return result


def _safe_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class Block(BaseModel):
    """DocumentIR 的内容块。

    id/order/source_locator 是定位与排序的唯一事实；text 是投影文本（表格为
    Markdown 表格投影，公式为 LaTeX/线性表达）；图片块通过 asset_ids 保留
    原始位置关系，不把媒体字节写进 IR。
    """

    id: str
    type: BlockType
    order: int
    text: str = ""
    level: int | None = None
    page: int | None = None
    bbox: list[float] | None = None
    parent_id: str | None = None
    caption: str | None = None
    asset_ids: list[str] = Field(default_factory=list)
    source_locator: str = ""

    @field_validator("bbox", mode="before")
    @classmethod
    def _v_bbox(cls, value: Any) -> Any:
        return _normalized_bbox(value)

    @field_validator("text", "caption", "source_locator", mode="before")
    @classmethod
    def _v_text(cls, value: Any) -> str:
        return _safe_text(value)


class Asset(BaseModel):
    """候选媒体资源。id 由材料 ID + 来源 locator + 内容 hash 派生。

    无 Vision 模式靠 caption/文件名/surrounding_text/bbox 的稳定启发式决定
    是否使用；有 Vision 模式由 Agent 读取图片后判断。
    """

    id: str
    path: str
    mime_type: str
    sha256: str
    width: int | None = None
    height: int | None = None
    page: int | None = None
    bbox: list[float] | None = None
    caption: str | None = None
    surrounding_text: str = ""
    source_locator: str = ""

    @field_validator("bbox", mode="before")
    @classmethod
    def _v_bbox(cls, value: Any) -> Any:
        return _normalized_bbox(value)

    @field_validator("path", "mime_type", "caption", "surrounding_text", "source_locator", mode="before")
    @classmethod
    def _v_text(cls, value: Any) -> str:
        return _safe_text(value)


class DocumentIR(BaseModel):
    """单个材料的统一中间表示（schema_version 版本化 JSON）。

    content.md 由 IR 单向投影生成，供 Agent 顺序阅读；它不是事实真源，
    不能反向解析覆盖 document.json（实施计划 §8.1 约束）。
    """

    schema_version: str = SCHEMA_VERSION
    material_id: str
    original_name: str
    source_format: SourceFormat
    sha256: str
    page_count: int | None = None
    blocks: list[Block] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _v_version(cls, value: Any) -> str:
        if str(value) != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {value}")
        return SCHEMA_VERSION

    def block_by_id(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def asset_by_id(self, asset_id: str) -> Asset | None:
        return next((a for a in self.assets if a.id == asset_id), None)

    def asset_ids_in_use(self) -> set[str]:
        """所有图片块引用的 Asset id 集合（用于校验遗漏/重复）。"""
        used: set[str] = set()
        for block in self.blocks:
            if block.type == "image":
                used.update(block.asset_ids)
        return used


class Selection(BaseModel):
    """Agent 的明确内容选择：source_id + purpose + target + transform。

    每个被读取且具有迁移价值的 Asset 必须出现在 selections 或 exclusions 中
    （实施计划 §8.2 约束）。不引入浮点相关性分数。
    """

    source_id: str
    purpose: str
    target: str
    transform: TransformKind = "preserve"


class Exclusion(BaseModel):
    source_id: str
    reason: ExclusionReason = "irrelevant"


class ContentPlan(BaseModel):
    """Agent 必须先写 work/plans/content-plan.json，Renderer 不直接猜测。

    mode 区分 vision / conservative；无 Vision 模式必须为 conservative，
    且 QAReport.visual 保持 not_run。
    """

    schema_version: str = SCHEMA_VERSION
    task_type: TaskType
    mode: PlanningMode
    selections: list[Selection] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    questions_asked: bool = False

    @field_validator("schema_version")
    @classmethod
    def _v_version(cls, value: Any) -> str:
        if str(value) != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {value}")
        return SCHEMA_VERSION

    def excluded_ids(self) -> set[str]:
        return {ex.source_id for ex in self.exclusions}


class QAReport(BaseModel):
    """任务级质量状态：机械门是硬门，视觉门只补充语义/审美判断。

    写任务日志并作为前端状态来源；不作为独立用户产物发布（实施计划 §8.3）。
    """

    mechanical: MechanicalStatus = "failed"
    mechanical_issues: list[str] = Field(default_factory=list)
    visual: QAStatus = "not_run"
    visual_issues: list[str] = Field(default_factory=list)
    repair_rounds: int = 0
    used_assets: list[str] = Field(default_factory=list)
    skipped_assets: list[str] = Field(default_factory=list)
