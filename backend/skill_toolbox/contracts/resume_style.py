"""简历中的可选链接与有限强调样式；不把 Word 格式细节暴露给 agent。"""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emphasis: Literal["normal", "bold", "accent", "bold_accent"] = Field(
        default="normal", description="普通、加粗、模板主题色、主题色并加粗；只强调重要亮点。")
    link: str = Field(default="", description="用户提供的 http/https 链接；不搜索、不编造。")

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
    label: str = Field(min_length=1, description="例如 GitHub、Stars、Forks、下载量；排在条目标题和技术栈下方。")
    value: str = Field(min_length=1, description="用户提供的值；数字不得推测或编造。")
