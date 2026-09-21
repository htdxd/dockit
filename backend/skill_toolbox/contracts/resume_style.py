"""简历中的可选链接与有限强调样式；不把 Word 格式细节暴露给 agent。"""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emphasis: Literal["normal", "bold", "accent", "bold_accent"] = Field(
        default="normal", description="普通、加粗、模板主题色、主题色并加粗；只强调重要亮点。")
    link: str = Field(default="", description="用户提供的 http/https 链接；不搜索、不编造。")
    background: bool = Field(default=False, description="浅色底纹，独立于加粗和主题色；只用于少量关键成果。")

    @field_validator("value", mode="before", check_fields=False)
    @classmethod
    def number_as_text(cls, value):
        return str(value) if type(value) in (int, float) else value

    @field_validator("link")
    @classmethod
    def valid_link(cls, value):
        if value and (urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc
                      or any(char in value for char in ('"', '\n', '\r'))):
            raise ValueError("链接必须是完整的 http/https 网址")
        return value


class DetailField(FieldStyle):
    label: str = Field(min_length=1, description="例如 GitHub、Stars、Forks、下载量；Stars/Forks 在项目标题行，其余按 layout 排在技术栈之后。")
    value: str = Field(min_length=1, description="用户提供的值；数字不得推测或编造。")
    layout: Literal["auto", "inline", "row"] = Field(default="auto", description="项目首行按名称、GitHub 简写 owner/repo、Stars/Forks、性质排序；其他 inline 字段合并同行，row 显式单独一行。实际由 Word 测量换行。")


class EntryHighlight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["organization", "role", "date", "tech_stack", "text"]
    paragraph: int = Field(default=0, ge=0, description="仅 text 使用：正文段落序号，从 0 开始。")
    text: str = Field(default="", description="精确原文片段；text 正文必须填写，其他字段省略则强调整个字段。每个目标内必须唯一匹配。")
    emphasis: Literal["normal", "bold", "accent", "bold_accent"] = "bold"
    background: bool = False

    @model_validator(mode="after")
    def validate_target(self):
        if self.field == "text" and not self.text.strip():
            raise ValueError("正文强调必须提供精确原文片段 text")
        if self.field != "text" and self.paragraph:
            raise ValueError("paragraph 仅用于 text 正文")
        return self
