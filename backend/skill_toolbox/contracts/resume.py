"""简历领域 typed contracts（实施计划 §5.3）。

所有字段均来自模板 manifest 白名单；``changes[]`` 只允许有限布局动作
（move_rows/resize_rows 由后端转换为安全 pt 并检查页面边界与重叠）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from skill_toolbox.contracts.resume_style import DetailField, FieldStyle, EntryHighlight

ResumeAction = Literal[
    "replace_text", "replace_asset", "resize_component", "shift_components",
    "clone_component",
]

# fields 顶层 key 白名单：manifest 字段 key + 派生键（photo/target_role）
_RESERVED_FIELD_KEYS = frozenset({"photo", "target_role"})


class ResumeChange(BaseModel):
    """受限组件动作。resize_rows/move_rows 由后端换算为安全 pt 动作。"""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    action: ResumeAction
    component_id: str = ""
    text: str = ""
    # 布局动作（行数 → pt 换算在后端）：move_rows 纵向移动 N 行，resize_rows
    # 把组件高度扩展 N 行（每行按组件行高估算）。
    move_rows: int = 0
    resize_rows: int = 0
    asset_id: str = ""
    after: str = ""


class ResumeGenerateRequest(BaseModel):
    """resume_generate 的内部契约。fields 由 llm_tools 做白名单校验后传入。"""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    template_id: str
    fields: dict[str, str] = Field(default_factory=dict)
    photo_asset_id: str = ""
    target_role: str = ""
    formats: list[str] = Field(default_factory=lambda: ["docx"])

    @field_validator("formats")
    @classmethod
    def _v_formats(cls, value: list[str]) -> list[str]:
        allowed = {"docx"}
        unknown = [fmt for fmt in value if fmt not in allowed]
        if unknown:
            raise ValueError(f"formats 只允许 {sorted(allowed)}，收到 {sorted(unknown)}")
        return value


class ResumeChangeRequest(BaseModel):
    artifact_id: str
    changes: list[ResumeChange] = Field(default_factory=list)


# ---------------- v2：内容模型 / 编辑动作 / 版本（P2-3，schema 冻结稿 §1–§4） ----------------

CONTENT_SCHEMA_V2 = "resume-content-v2"

# 组件原型：由模板包实测导出（P2-1 的角色分桶一一对应）
ResumePrototype = Literal["experience_v1", "plain_lines_v1"]
ResumeLayoutMode = Literal["reflow", "local"]

ResumeEditOpV2 = Literal[
    "update_entry", "insert_entry", "remove_entry", "move_entry",
    "insert_section", "remove_section", "update_section_title",
    "set_entry_gap", "move_component", "resize_component", "format_component",
]


class ResumeEntryV2(BaseModel):
    """条目：experience_v1 用 head+bullets，plain_lines_v1 用 lines。

    head 三槽位（date/org/role）由模板列位渲染；空槽位不写空白字符。
    id 可省略（后端按 `<栏目key>-<序号>` 生成）——模型常不写 id，强制必填
    会把本可渲染的请求变成参数错误。
    """

    id: str = ""
    head: dict[str, str] = Field(default_factory=dict)
    bullets: list[str] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)
    tech_stack: str = ""
    details: list[DetailField] = Field(default_factory=list)
    highlights: list[EntryHighlight] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    font_size_pt: float | None = Field(default=None, ge=8, le=18)
    scale: float | None = Field(default=None, ge=0.75, le=1.25)


class ResumeSectionV2(BaseModel):
    key: str
    title: str
    prototype: ResumePrototype = "experience_v1"
    entries: list[ResumeEntryV2] = Field(default_factory=list)
    entry_gap_pt: float | None = None
    scale: float | None = Field(default=None, ge=0.75, le=1.25)


class ResumePersonalField(FieldStyle):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: str


class ResumeHeaderV2(BaseModel):
    """母版个人信息与照片（P2 修订）。

    `fields` 的键必须是模板信息区真实存在的标签映射（name/ethnicity/phone/
    email/address/birth/height/politics/school/degree）；未知键**显式拒绝**，
    不静默丢弃。`photo_path` 是相对 workspace 的图片路径。
    """

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, str] = Field(default_factory=dict)
    photo_path: str = ""
    hide_photo: bool = False
    hidden_fields: list[str] = Field(default_factory=list)
    custom_fields: list[ResumePersonalField] = Field(default_factory=list)


class ResumeContentV2(BaseModel):
    density: Literal["normal", "compact"] = "normal"
    schema_version: Literal["resume-content-v2"] = "resume-content-v2"
    template_id: str
    layout_mode: ResumeLayoutMode = "reflow"
    header: ResumeHeaderV2 | None = None
    sections: list[ResumeSectionV2] = Field(default_factory=list)


class ResumeEditV2(BaseModel):
    """单条编辑动作（语义优先；几何动作仅局部精调）。

    几何参数语义（**不支持的一律显式拒绝，绝不静默丢弃**）：
    - move_component：`dx_pt`/`dy_pt`（正文禁止横向位移，dx 必须为 0）
    - resize_component：`height_pt`（绝对框高）/`height_delta_pt`（增量）/
      `width_pt`（绝对宽，变窄必须重新测量换行）
    """

    model_config = ConfigDict(extra="forbid")

    op: ResumeEditOpV2
    instance_id: str = ""
    section_key: str = ""
    entry_id: str = ""
    entry: ResumeEntryV2 | None = None
    section: ResumeSectionV2 | None = None
    title: str = ""
    after: str = ""
    before: str = ""
    gap_pt: float | None = None
    dx_pt: float = 0.0
    dy_pt: float = 0.0
    width_pt: float | None = None
    height_pt: float | None = None
    height_delta_pt: float | None = None
    font_size_pt: float | None = Field(default=None, ge=8, le=18)
    scale: float | None = Field(default=None, ge=0.75, le=1.25)


class ResumeGenerateV2Request(BaseModel):
    template_id: str
    content: ResumeContentV2
    request_id: str = ""
    layout_mode: ResumeLayoutMode = "reflow"


class ResumeRepairV2Request(BaseModel):
    density: Literal["normal", "compact"] | None = None
    artifact_id: str
    base_revision: int
    header: ResumeHeaderV2 | None = None
    request_id: str
    changes: list[ResumeEditV2] = Field(default_factory=list)
    layout_mode: ResumeLayoutMode | None = None


class ResumeAcceptRequest(BaseModel):
    artifact_id: str
    candidate_revision: int
    expected_accepted_revision: int | None = None
    request_id: str = ""


class ResumeRestoreRequest(BaseModel):
    artifact_id: str
    target_revision: int
    expected_accepted_revision: int
    request_id: str = ""


class ResumePreviewRequest(BaseModel):
    artifact_id: str
    revision: int
    pages: list[int] = Field(default_factory=list)
