"""面向 Agent 的简历工作流契约；模板与版本细节由后端管理。"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FontSize = Annotated[float, Field(ge=8, le=18)]
ComponentScale = Annotated[float, Field(ge=0.75, le=1.25)]


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepareRequest(WorkflowModel):
    material_ids: list[str] | None = None


class EntryPatch(WorkflowModel):
    """只修改显式提供的字段；空 text 可用于清空原正文。"""

    date: str = Field(default="", description="原材料中的起止日期，不编造日期；与机构、角色分开填写")
    organization: str = Field(default="", description="学校、公司或项目名称；不拼接日期或角色。技能/自评等纯文字条目只填 text，不把栏目标题填在这里")
    role: str = Field(default="", description="专业、岗位或项目性质（如公司项目、个人项目）；作为标题信息，不混入正文")
    text: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list, description="可选：本条经历的事实来源 ID，复制 prepare 的 source_id 或问答返回值；只改表达时省略以保留原引用，不填写文件路径")
    tech_stack: str = Field(default="", description="项目技术栈，单独排在项目名称/性质下方；用逗号分隔，不放入 role")

    @field_validator("tech_stack", mode="before")
    @classmethod
    def technology_text(cls, value):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return "、".join(value)
        return value
    font_size_pt: FontSize | None = Field(default=None, description="缩放前的正文字号（pt）；省略或 null 保留模板或已有字号")
    scale: ComponentScale | None = Field(default=None, description="条目整体缩放比例；1 为原大小，绝对设置不累乘；省略或 null 保留；最终字号不得小于 8pt 且须放得下页面")


class Entry(EntryPatch):
    """完整条目：日期、机构、角色或正文至少一项有实际内容；id 可省略。"""

    id: str = ""

    @model_validator(mode="after")
    def require_content(self):
        if not any(value.strip() for value in (self.date, self.organization, self.role, self.tech_stack, *self.text)):
            raise ValueError("条目必须包含标题信息或正文")
        return self


class Section(WorkflowModel):
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    entries: list[Entry] = Field(min_length=1)
    scale: ComponentScale | None = Field(default=None, description="栏目整体缩放，包含标题、图标和条目，不影响个人信息和照片；1 为原大小，最终字号至少 8pt 且须放得下页面")


class PersonalField(WorkflowModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: str


class Content(WorkflowModel):
    person: dict[str, str] = Field(default_factory=dict)
    personal_fields: list[PersonalField] = Field(default_factory=list)
    photo_asset_id: str = ""
    sections: list[Section] = Field(min_length=1)


class GenerateRequest(WorkflowModel):
    density: Literal["normal", "compact"] = Field(default="normal", description="一页且内容较多时选 compact：保持正文宽度，压缩纵向间距；不删文字。normal 保留原间距。")
    content: Content = Field(description="简历内容对象；sections、person 等必须放在 content 内，不放在请求顶层")
    target_pages: int = Field(default=1, ge=1, le=3, description="允许的最大页数；不会为了达到此值填满页面")
    font_size_pt: FontSize | None = Field(default=None, description="所有未单独指定字号条目的缩放前正文字号（pt）")


class UpdateEntry(WorkflowModel):
    op: Literal["update_entry"]
    target_id: str
    entry: EntryPatch


class InsertEntry(WorkflowModel):
    op: Literal["insert_entry"]
    section_id: str
    entry: Entry
    after_id: str | None = None


class RemoveEntry(WorkflowModel):
    op: Literal["remove_entry"]
    target_id: str


class MoveEntry(WorkflowModel):
    op: Literal["move_entry"]
    target_id: str
    before_id: str | None = None
    after_id: str | None = None

    @model_validator(mode="after")
    def require_destination(self):
        if bool(self.before_id) == bool(self.after_id):
            raise ValueError("移动条目必须且只能指定 before_id 或 after_id")
        return self


class InsertSection(WorkflowModel):
    op: Literal["insert_section"]
    section: Section
    after_id: str | None = None


class RemoveSection(WorkflowModel):
    op: Literal["remove_section"]
    section_id: str


class RenameSection(WorkflowModel):
    op: Literal["rename_section"]
    section_id: str
    title: str = Field(min_length=1)


class MoveSection(WorkflowModel):
    op: Literal["move_section"]
    section_id: str
    before_id: str | None = None
    after_id: str | None = None

    @model_validator(mode="after")
    def require_destination(self):
        if bool(self.before_id) == bool(self.after_id):
            raise ValueError("移动栏目必须且只能指定 before_id 或 after_id")
        if self.section_id in (self.before_id, self.after_id):
            raise ValueError("不能把栏目移动到自身之前或之后")
        return self


class UpdatePerson(WorkflowModel):
    op: Literal["update_person"]
    fields: dict[str, str]


class ReplacePhoto(WorkflowModel):
    op: Literal["replace_photo"]
    asset_id: str


class SetPersonalField(WorkflowModel):
    op: Literal["set_person_field"]
    field: PersonalField


class RemovePersonalField(WorkflowModel):
    op: Literal["remove_person_field"]
    key: str


class Format(WorkflowModel):
    op: Literal["format"]
    scope: Literal["all", "section", "entry"]
    target_id: str = Field(default="", description="section 填栏目 key；entry 填条目 target_id；all 省略")
    font_size_pt: FontSize | None = Field(default=None, description="所选条目缩放前的正文字号（pt）；省略或 null 不改变")
    scale: ComponentScale | None = Field(default=None, description="整体缩放绝对比例；section/all 包含栏目标题图标但不改个人信息或照片，entry 只缩放条目；最终字号至少 8pt 且须放得下页面；省略或 null 不改变")

    @model_validator(mode="after")
    def require_format(self):
        if self.font_size_pt is None and self.scale is None:
            raise ValueError("format 至少指定 font_size_pt 或 scale")
        if self.scope != "all" and not self.target_id.strip():
            raise ValueError("栏目或条目格式操作必须指定 target_id")
        return self


Change = Annotated[
    UpdateEntry | InsertEntry | RemoveEntry | MoveEntry | InsertSection
    | RemoveSection | RenameSection | MoveSection | UpdatePerson | ReplacePhoto
    | SetPersonalField | RemovePersonalField | Format,
    Field(discriminator="op"),
]


class EditRequest(WorkflowModel):
    density: Literal["normal", "compact"] | None = Field(default=None, description="可单独设 compact 压页：将已有等比缩放折算为等效字号并恢复完整宽度，压缩纵向留白；正文和照片不变。")
    candidate_id: str = Field(description="每次调用必填：原样复制最近工具结果的 candidate_id，不能因前次已传而省略")
    changes: list[Change] = Field(default_factory=list, description="原生 JSON 数组，数组元素是动作对象；不要把数组编码成字符串或放入代码块")

    @field_validator("changes", mode="before")
    @classmethod
    def decode_changes_array(cls, value):
        if not isinstance(value, str):
            return value
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"changes 字符串不是完整 JSON：位置 {exc.pos}（第 {exc.lineno} 行第 {exc.colno} 列），"
                f"{exc.msg}。请重发原生 changes 数组；长批次拆为每次一个动作，"
                "沿用实际 candidate_id 和 target_id，不要原样重试错误字符串。"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError("changes 必须是动作数组；JSON 字符串解码后也必须是数组。")
        return decoded
    target_pages: int | None = Field(default=None, ge=1, le=3, description="修改允许的最大页数；可单独使用，不强制填满页面")

    @model_validator(mode="after")
    def require_edit(self):
        if not self.changes and self.target_pages is None and self.density is None:
            raise ValueError("至少提供 changes、density 或 target_pages")
        return self


class PreviewRequest(WorkflowModel):
    candidate_id: str
    pages: list[int] | None = None


class AcceptRequest(WorkflowModel):
    candidate_id: str
    visual_notes: str = Field(min_length=1)
