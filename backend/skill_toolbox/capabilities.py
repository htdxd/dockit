"""Model capability resolution for the skill runtime.

Skills run under a user-configured LLM whose feature set varies (vision support,
strict tool calling, JSON schema adherence). Skills declare ``required_capabilities``
and ``optional_capabilities`` in their manifest; the sidecar resolves what the
configured model actually provides and either rejects the task explicitly
(required capability missing — never a silent degradation) or routes the agent
to the appropriate flow via a capability banner in the system prompt.

Resolution priority for each capability:
1. Explicit user override in ``ProviderConfig`` (highest authority).
2. Static model-name table (regex match against the configured model string).
3. Safe default (``False`` for vision — a non-vision model must never be
   allowed to claim visual verification).

``tool_calling`` and ``json_schema`` are assumed present: the runtime's
tool loop and JSON tool schemas require them, and a model without them simply
fails at the API layer with a provider error rather than degrading silently.
"""

from __future__ import annotations

import re

from skill_toolbox.models import ProviderConfig

# Capabilities the runtime machinery itself requires to function at all.
ASSUMED_PRESENT = ("tool_calling", "json_schema")

# Static vision model table. Patterns are matched (case-insensitively) against
# the configured model name. Keep the list tight: a false positive lets a
# non-vision model claim visual verification it cannot perform.
_VISION_MODEL_PATTERNS = (
    # OpenAI multimodal
    r"gpt-4o",
    r"gpt-4\.1",
    r"gpt-4\.5",
    r"gpt-4-vision",
    r"gpt-4-turbo",
    r"gpt-5",
    # Google Gemini — all Gemini models are multimodal
    r"gemini",
    # Anthropic Claude — 3 / 3.5 / 3.7 / 4 families all accept images
    r"claude-3",
    r"claude-4",
    # Alibaba Qwen-VL
    r"qwen[0-9.\-]*vl",
    r"qwen-vl",
    # Zhipu GLM vision (glm-4v / glm-4.1v / glm-4.5v)
    r"glm-[0-9.]+v",
    # Volcengine Doubao vision
    r"doubao[0-9.\-]*vision",
    # MiniMax VL
    r"minimax[-_]vl",
    # Open-source / open-weight vision models
    r"llava",
    r"internvl",
    r"intern-vl",
    r"deepseek-vl",
    r"deepseek-ocr",
    r"phi-3\.5-vision",
    r"phi-4-multimodal",
    r"pixtral",
    # Others (Yi / Step / Hunyuan)
    r"yi-vision",
    r"step-[12]v",
    r"hunyuan[0-9.\-]*vision",
)

_VISION_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _VISION_MODEL_PATTERNS)


def is_vision_model(model: str) -> bool:
    """Match a model name against the static vision table (no user override)."""
    name = (model or "").strip()
    return any(pattern.search(name) for pattern in _VISION_RE)


def resolve_vision_capable(config: ProviderConfig) -> bool:
    """Resolve vision capability: user override wins, then the static table."""
    if config.vision is not None:
        return config.vision
    return is_vision_model(config.model)


def resolve_capabilities(config: ProviderConfig) -> dict[str, bool]:
    """Resolve the full capability set advertised to a skill's system prompt.

    tool_calling / json_schema default to True (the runtime's tool loop and
    JSON tool schemas require them) but can be explicitly overridden by the
    user (e.g. a strict OpenAI-compatible gateway that rejects tool calling).
    """
    return {
        "tool_calling": config.tool_calling if config.tool_calling is not None else True,
        "json_schema": config.json_schema if config.json_schema is not None else True,
        "vision": resolve_vision_capable(config),
    }


def missing_required(
    required: frozenset[str], capabilities: dict[str, bool]
) -> list[str]:
    """Capabilities a skill requires but the model does not provide (sorted)."""
    return sorted(capability for capability in required if not capabilities.get(capability, False))
