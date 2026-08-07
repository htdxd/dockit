import pytest
from skill_toolbox.capabilities import (
    anthropic_reasoning_budget,
    is_vision_model,
    load_probe_report,
    missing_required,
    openai_reasoning_effort,
    probe_fingerprint,
    resolve_capabilities,
    resolve_vision_capable,
)
from skill_toolbox.models import (
    CapabilityProbeReport,
    ProbeResult,
    ProviderConfig,
)


def _config(
    model: str,
    vision: bool | None = None,
    tool_calling: bool | None = None,
    reasoning_level: str = "auto",
    capability_probe: str = "",
) -> ProviderConfig:
    return ProviderConfig(
        kind="openai",
        model=model,
        vision=vision,
        tool_calling=tool_calling,
        reasoning_level=reasoning_level,
        capability_probe=capability_probe,
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
    assert capabilities["tool_calling"] is True
    # json_schema 已移除（工具参数 schema 归入 Tool Calling）
    assert "json_schema" not in capabilities

    capabilities = resolve_capabilities(_config("deepseek-chat"))
    assert capabilities["vision"] is False


def test_user_override_for_tool_calling() -> None:
    # Explicit override wins; None falls back to the default True.
    assert resolve_capabilities(_config("gpt-4o", tool_calling=False))["tool_calling"] is False
    assert resolve_capabilities(_config("gpt-4o", tool_calling=True))["tool_calling"] is True


def test_missing_required_reports_only_absent_capabilities() -> None:
    caps = {"tool_calling": True, "vision": False}
    assert missing_required(frozenset({"tool_calling"}), caps) == []
    assert missing_required(frozenset({"vision"}), caps) == ["vision"]
    assert missing_required(frozenset({"tool_calling", "vision"}), caps) == ["vision"]
    assert missing_required(frozenset(), caps) == []


# ===== 推理档位映射 =====

def test_reasoning_level_maps_to_openai_effort() -> None:
    assert openai_reasoning_effort("auto") is None  # auto 不发送参数
    assert openai_reasoning_effort("fast") == "low"
    assert openai_reasoning_effort("balanced") == "medium"
    assert openai_reasoning_effort("deep") == "high"


def test_reasoning_level_maps_to_anthropic_budget() -> None:
    assert anthropic_reasoning_budget("auto") is None
    assert anthropic_reasoning_budget("fast") == 2048
    assert anthropic_reasoning_budget("balanced") == 4096
    assert anthropic_reasoning_budget("deep") == 8192


def test_thinking_kwargs_budget_respects_1024_lower_bound() -> None:
    """budget_tokens 必须在 [1024, max_tokens) 内；max_tokens 过小时回退 auto
    （不构造必然 400 的请求）。"""
    from skill_toolbox.providers.anthropic import _thinking_kwargs

    # fast → 2048，低于 1024 会被抬到 1024；deep → 8192 正常
    assert _thinking_kwargs("fast", 4096) == {
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "temperature": 1,
    }
    # deep(8192) >= max_tokens(8192) → 无法满足 < max_tokens，回退 auto
    assert _thinking_kwargs("deep", 8192) == {}
    # 512 < 1024 → 即使 fast 也抬不到 1024 以下，回退 auto
    assert _thinking_kwargs("fast", 512) == {}
    assert _thinking_kwargs("auto", 8192) == {}


# ===== 能力探测报告解析（宽容策略） =====

def test_empty_probe_loads_as_all_unknown() -> None:
    report = load_probe_report(_config("gpt-4o"))
    assert report.tool_calling.status == "unknown"
    assert report.vision.status == "unknown"
    assert report.reasoning_control.status == "unknown"


def test_probe_report_parse_is_lenient_for_garbage() -> None:
    for bad in ("{bad json", "[]", "null", "42"):
        report = load_probe_report(_config("gpt-4o", capability_probe=bad))
        assert report.tool_calling.status == "unknown"


def test_verified_probe_overrides_static_table() -> None:
    rpt = CapabilityProbeReport(
        vision=ProbeResult(status="verified"),
    )
    # deepseek-chat 静态表判非视觉；探测 verified 后应视为视觉
    assert resolve_capabilities(_config("deepseek-chat", capability_probe=rpt.model_dump_json()))["vision"] is True


def test_unsupported_probe_overrides_static_table() -> None:
    rpt = CapabilityProbeReport(
        vision=ProbeResult(status="unsupported"),
    )
    # gpt-4o 静态表判视觉；探测 unsupported 后应视为非视觉
    assert resolve_capabilities(_config("gpt-4o", capability_probe=rpt.model_dump_json()))["vision"] is False


def test_probe_error_falls_back_to_static_table() -> None:
    rpt = CapabilityProbeReport(
        vision=ProbeResult(status="probe_error", detail="timeout"),
    )
    # probe_error 不改变静态表判定
    assert resolve_capabilities(_config("gpt-4o", capability_probe=rpt.model_dump_json()))["vision"] is True


def test_user_override_beats_probe() -> None:
    rpt = CapabilityProbeReport(vision=ProbeResult(status="unsupported"))
    # 用户显式 override 优先于探测结果
    assert resolve_capabilities(_config("gpt-4o", vision=True, capability_probe=rpt.model_dump_json()))["vision"] is True


def test_stale_probe_version_treated_as_unknown() -> None:
    rpt = CapabilityProbeReport(
        vision=ProbeResult(status="verified", probe_version=0),
    )
    # 旧版本探测不参与解析（仍可展示），deepseek-chat 回落到静态表
    assert resolve_capabilities(_config("deepseek-chat", capability_probe=rpt.model_dump_json()))["vision"] is False


def test_probe_fingerprint_invalidates_on_kind_base_or_model_change() -> None:
    base = _config("gpt-4o")
    # vision override 不属于探测指纹（改它不该使探测失效）
    assert probe_fingerprint(base) == probe_fingerprint(_config("gpt-4o", vision=True))
    assert probe_fingerprint(base) != probe_fingerprint(_config("gpt-4o-mini"))  # model 变
    other = _config("gpt-4o")
    other.kind = "anthropic"
    assert probe_fingerprint(base) != probe_fingerprint(other)
    other2 = _config("gpt-4o")
    other2.base_url = "https://api.example.com/v1"
    assert probe_fingerprint(base) != probe_fingerprint(other2)


def test_reasoning_level_is_not_a_skill_gate() -> None:
    caps = resolve_capabilities(_config("deepseek-chat", reasoning_level="deep"))
    # reasoning_control 是内部能力，不出现在任务门槛横幅
    assert "reasoning_control" not in caps
