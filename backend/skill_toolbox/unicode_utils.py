from __future__ import annotations

import re
from typing import Any

_UNPAIRED_SURROGATE = re.compile(r"[\ud800-\udfff]")

# 能力探测错误文本里的凭据泄漏模式：OpenAI/Anthropic 风格的 sk-* 密钥、
# JSON 里的 "api_key"/"key" 字段值、URL 中的 key 查询参数。
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|key|token)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"(?i)([?&](?:key|token|api[_-]?key)=)[^&\s\"']+"),
)


def redact_secrets(text: str) -> str:
    """Strip API keys / tokens from error or detail text before it is emitted
    to the UI or persisted. Applied at every probe error path — never log or
    store raw credentials."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def sanitize_text(value: str) -> str:
    """Replace code points that cannot be encoded as UTF-8."""
    return _UNPAIRED_SURROGATE.sub("\ufffd", value)


def sanitize_data(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_data(key): sanitize_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_data(item) for item in value)
    return value
