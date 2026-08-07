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


# ===== 能力探测共享类型 =====
# 三个状态集合是后端与前端共用的协议词汇，避免 Sidecar / UI 各自发明字符串。
# 探测结果是版本化 JSON，解析必须容忍空值、旧版本与未知字段（见 capabilities.py）。

CapabilityStatus = Literal["unknown", "verified", "unsupported", "probe_error"]
ReasoningControl = Literal["none", "effort", "budget", "adaptive"]
ReasoningLevel = Literal["auto", "fast", "balanced", "deep"]


class ProbeResult(BaseModel):
    status: CapabilityStatus = "unknown"
    checked_at: float = 0.0
    probe_version: int = 1
    detail: str | None = None
    # Reasoning Control 探测专有：验证成功的 control 类型。effort = OpenAI
    # reasoning_effort；budget = Anthropic budget_tokens；adaptive = Claude
    # thinking.type=adaptive。OpenAI-compatible 网关若无可验证 metadata，
    # 保持 unknown（见 capabilities.py probe_reasoning_control）。
    control: ReasoningControl | None = None


class CapabilityProbeReport(BaseModel):
    tool_calling: ProbeResult = Field(default_factory=ProbeResult)
    vision: ProbeResult = Field(default_factory=ProbeResult)
    reasoning_control: ProbeResult = Field(default_factory=ProbeResult)


class ProviderConfig(BaseModel):
    kind: Literal["openai", "anthropic", "openai_compatible", "mock"]
    model: str
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = Field(default=16384, ge=256, le=32768)
    # Explicit user overrides for model capabilities. None = unknown, resolved
    # from defaults / the static model table in skill_toolbox.capabilities.
    vision: bool | None = None
    tool_calling: bool | None = None
    # 推理强度档位：auto=不主动发送 reasoning 参数（用 Provider 默认行为），
    # fast/balanced/deep 由各 adapter 映射为厂商原生参数。
    reasoning_level: Literal["auto", "fast", "balanced", "deep"] = "auto"
    # 版本化能力探测报告 JSON（CapabilityProbeReport 的序列化结果）。SQLite
    # 以 TEXT 存储；解析必须容忍空值/旧版本/未知字段。kind/base_url/model 变化
    # 时由调用方重置为 unknown（见 capabilities.py probe_fingerprint）。
    capability_probe: str = ""
