from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from skill_toolbox.unicode_utils import sanitize_data, sanitize_text


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def sanitize_arguments(cls, value: Any) -> Any:
        return sanitize_data(value)


class AssistantTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def sanitize_content(cls, value: Any) -> Any:
        return sanitize_text(value) if isinstance(value, str) else value


class ImageContent(BaseModel):
    media_type: str
    base64_data: str


class ToolResult(BaseModel):
    tool_call_id: str
    name: str
    success: bool
    content: str
    images: list[ImageContent] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def sanitize_content(cls, value: Any) -> Any:
        return sanitize_text(value) if isinstance(value, str) else value


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def sanitize_content(cls, value: Any) -> Any:
        return sanitize_text(value) if isinstance(value, str) else value


class ProviderConfig(BaseModel):
    kind: Literal["openai", "anthropic", "openai_compatible", "mock"]
    model: str
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = Field(default=16384, ge=256, le=32768)
