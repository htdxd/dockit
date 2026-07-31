from __future__ import annotations

import re
from typing import Any

_UNPAIRED_SURROGATE = re.compile(r"[\ud800-\udfff]")


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
