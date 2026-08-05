import pytest

from skill_toolbox.capabilities import (
    ASSUMED_PRESENT,
    is_vision_model,
    missing_required,
    resolve_capabilities,
    resolve_vision_capable,
)
from skill_toolbox.models import ProviderConfig


def _config(
    model: str,
    vision: bool | None = None,
    tool_calling: bool | None = None,
    json_schema: bool | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        kind="openai",
        model=model,
        vision=vision,
        tool_calling=tool_calling,
        json_schema=json_schema,
    )


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4-vision-preview",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus",
        "claude-4-sonnet",
        "qwen-vl-plus",
        "qwen2.5-vl-72b-instruct",
        "glm-4v",
        "glm-4.5v",
        "doubao-1.5-vision-pro-32k-250115",
        "minimax-VL-01",
        "llava-v1.6-34b",
        "pixtral-12b",
        "deepseek-vl2",
    ],
)
def test_is_vision_model_matches_known_multimodal_models(model: str) -> None:
    assert is_vision_model(model)


@pytest.mark.parametrize(
    "model",
    [
        "gpt-3.5-turbo",
        "gpt-4",
        "gpt-4-0613",
        "claude-2.1",
        "claude-instant-1.2",
        "text-davinci-003",
        "qwen-plus",
        "qwen2.5-72b-instruct",
        "glm-4",
        "glm-4.5",
        "doubao-pro-32k",
        "deepseek-chat",
        "deepseek-r1",
        "llama-3.1-70b",
        "mistral-large",
        "o1-mini",
        "o3",
        "",
    ],
)
def test_is_vision_model_does_not_match_text_only_models(model: str) -> None:
    assert not is_vision_model(model)


def test_user_override_wins_over_static_table() -> None:
    # Non-vision model, user forces vision off/on explicitly.
    assert not resolve_vision_capable(_config("gpt-4o", vision=False))
    assert resolve_vision_capable(_config("deepseek-chat", vision=True))


def test_static_table_used_when_no_override() -> None:
    assert resolve_vision_capable(_config("gpt-4o"))
    assert not resolve_vision_capable(_config("deepseek-chat"))
    # None override behaves exactly like an absent field.
    assert resolve_vision_capable(_config("gemini-2.5-pro", vision=None))


def test_resolve_capabilities_shape() -> None:
    capabilities = resolve_capabilities(_config("gpt-4o"))
    assert capabilities["vision"] is True
    for name in ASSUMED_PRESENT:
        assert capabilities[name] is True

    capabilities = resolve_capabilities(_config("deepseek-chat"))
    assert capabilities["vision"] is False


def test_user_override_for_tool_calling_and_json_schema() -> None:
    # Explicit override wins; None falls back to the default True.
    assert resolve_capabilities(_config("gpt-4o", tool_calling=False))["tool_calling"] is False
    assert resolve_capabilities(_config("gpt-4o", tool_calling=True))["tool_calling"] is True
    assert resolve_capabilities(_config("gpt-4o", json_schema=False))["json_schema"] is False
    assert resolve_capabilities(_config("gpt-4o", json_schema=None))["json_schema"] is True


def test_missing_required_reports_only_absent_capabilities() -> None:
    caps = {"tool_calling": True, "json_schema": True, "vision": False}
    assert missing_required(frozenset({"tool_calling"}), caps) == []
    assert missing_required(frozenset({"vision"}), caps) == ["vision"]
    assert missing_required(frozenset({"tool_calling", "vision"}), caps) == ["vision"]
    assert missing_required(frozenset(), caps) == []
