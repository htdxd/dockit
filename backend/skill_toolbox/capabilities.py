"""Model capability resolution for the skill runtime.

Skills run under a user-configured LLM whose feature set varies (vision support,
strict tool calling, reasoning control). Skills declare ``required_capabilities``
and ``optional_capabilities`` in their manifest; the sidecar resolves what the
configured model actually provides and either rejects the task explicitly
(required capability missing — never a silent degradation) or routes the agent
to the appropriate flow via a capability banner in the system prompt.

Resolution priority for each capability:
1. Explicit user override in ``ProviderConfig`` (highest authority).
2. Latest successful probe for the same ``kind + base_url + model``
   (``ProviderConfig.capability_probe``, versioned JSON).
3. Static model-name table (regex match against the configured model string) —
   a hint only, never authoritative.
4. Safe default (``False`` for vision; Reasoning Control is never a gate).

``tool_calling`` is assumed present when unverified: the runtime's tool loop
requires it, and a model without it simply fails at the API layer with a
provider error rather than degrading silently. Vision stays optional.
Reasoning Control (``reasoning_control``) is an optional optimization, never a
skill startup requirement.

Probe reports are versioned JSON on the provider row. Parsing is lenient:
missing keys, old versions, and unknown fields fall back to ``unknown`` —
a corrupted probe must never block a task or the settings page.
"""

from __future__ import annotations

import json
import re

from skill_toolbox.models import (
    CapabilityProbeReport,
    ProviderConfig,
    ReasoningLevel,
)

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

# Reasoning level → vendor parameter mapping (per-adapter application in
# providers/*.py). auto = do not send any reasoning parameter.
REASONING_LEVELS: tuple[ReasoningLevel, ...] = ("auto", "fast", "balanced", "deep")

# OpenAI reasoning_effort values are a closed enum; Anthropic uses token budgets.
_OPENAI_EFFORT: dict[str, str] = {"fast": "low", "balanced": "medium", "deep": "high"}
# Anthropic budget_tokens must be >= 1024 and < max_tokens.
_ANTHROPIC_BUDGET: dict[str, int] = {"fast": 2048, "balanced": 4096, "deep": 8192}

_PROBE_VERSION = 1


def openai_reasoning_effort(level: ReasoningLevel) -> str | None:
    """Map a user reasoning level to an OpenAI ``reasoning_effort`` value."""
    return _OPENAI_EFFORT.get(level)


def anthropic_reasoning_budget(level: ReasoningLevel) -> int | None:
    """Map a user reasoning level to an Anthropic ``budget_tokens`` value."""
    return _ANTHROPIC_BUDGET.get(level)


def is_vision_model(model: str) -> bool:
    """Match a model name against the static vision table (no user override)."""
    name = (model or "").strip()
    return any(pattern.search(name) for pattern in _VISION_RE)


def resolve_vision_capable(config: ProviderConfig) -> bool:
    """Resolve vision capability: user override wins, then the static table."""
    if config.vision is not None:
        return config.vision
    return is_vision_model(config.model)


def load_probe_report(config: ProviderConfig) -> CapabilityProbeReport:
    """Parse ``capability_probe`` JSON leniently. Old reports (probe_version <
    current) are kept for display but treated as unknown for resolution — a
    stale probe from a previous code version must not gate a task."""
    if not config.capability_probe:
        return CapabilityProbeReport()
    try:
        raw = json.loads(config.capability_probe)
    except (TypeError, ValueError):
        return CapabilityProbeReport()
    if not isinstance(raw, dict):
        return CapabilityProbeReport()
    try:
        report = CapabilityProbeReport.model_validate(raw)
    except Exception:  # noqa: BLE001
        return CapabilityProbeReport()
    if report.tool_calling.probe_version != _PROBE_VERSION:
        report.tool_calling.status = "unknown"
    if report.vision.probe_version != _PROBE_VERSION:
        report.vision.status = "unknown"
    if report.reasoning_control.probe_version != _PROBE_VERSION:
        report.reasoning_control.status = "unknown"
    return report


def probe_fingerprint(config: ProviderConfig) -> str:
    """Identity of a provider's probeable surface: kind + base_url + model.

    Changing any of these invalidates a stored probe (see §6 of the plan);
    renaming the provider or editing the API key does not."""
    return json.dumps(
        [config.kind, (config.base_url or "").rstrip("/"), config.model],
        ensure_ascii=False,
        sort_keys=True,
    )


def resolve_capabilities(config: ProviderConfig) -> dict[str, bool]:
    """Resolve the full capability set advertised to a skill's system prompt.

    Priority: explicit user override → latest same-fingerprint probe →
    static table hint → safe default. tool_calling defaults to True when
    unverified (the runtime's tool loop requires it). vision defaults to the
    static table. reasoning_control is an internal flag (never a gate) — it is
    not advertised in the prompt banner.
    """
    report = load_probe_report(config)
    # tool_calling is a required runtime capability: user override wins, and it
    # is assumed present otherwise — the tool loop requires it, and a model
    # without it fails at the API layer rather than degrading silently. A probe
    # can only inform the UI (unsupported warning), never gate the task.
    tool_calling = config.tool_calling if config.tool_calling is not None else True
    vision = resolve_vision_capable(config)
    if config.vision is None:
        if report.vision.status == "verified":
            vision = True
        elif report.vision.status == "unsupported":
            vision = False
        # probe_error / unknown → fall through to static table
    return {
        "tool_calling": tool_calling,
        "vision": vision,
    }


def missing_required(
    required: frozenset[str], capabilities: dict[str, bool]
) -> list[str]:
    """Capabilities a skill requires but the model does not provide (sorted)."""
    return sorted(capability for capability in required if not capabilities.get(capability, False))
