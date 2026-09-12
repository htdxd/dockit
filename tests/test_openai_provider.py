from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from skill_toolbox.models import ProviderConfig
from skill_toolbox.providers import openai as openai_provider


class _FakeCompletions:
    def __init__(self, content: str = "2") -> None:
        self.calls: list[dict[str, object]] = []
        self.content = content

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(
                completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
            ),
        )


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _provider_with_response(monkeypatch, response):
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=AsyncMock(return_value=response)),
    ))
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", lambda **_: client)
    return openai_provider.OpenAIProvider(
        ProviderConfig(kind="openai", model="test", api_key="test")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "tool_calls"])
async def test_complete_records_safe_counts_and_invalid_json_reason(monkeypatch, finish_reason):
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(
                content=None,
                reasoning_content="hidden reasoning must not be recorded",
                tool_calls=[SimpleNamespace(id="call-1", function=SimpleNamespace(
                    name="resume_generate", arguments='{"content":',
                ))],
            ),
        )],
        usage=SimpleNamespace(
            prompt_tokens=120, completion_tokens=80, total_tokens=200,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=60),
            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            vendor_private="not included",
        ),
    )
    provider = _provider_with_response(monkeypatch, response)
    turn = await provider.complete("system", [], [])
    assert turn.response_metadata == {
        "finish_reason": finish_reason,
        "usage": {
            "prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200,
            "reasoning_tokens": 60, "cached_tokens": 20,
        },
    }
    assert turn.tool_calls[0].arguments == {"_invalid_json": '{"content":', "_finish_reason": finish_reason}
    assert "hidden reasoning" not in turn.model_dump_json()
    assert "vendor_private" not in turn.model_dump_json()


@pytest.mark.asyncio
async def test_complete_accepts_response_without_usage_or_finish_reason(monkeypatch):
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=[]),
    )])
    provider = _provider_with_response(monkeypatch, response)
    turn = await provider.complete("system", [], [])
    assert turn.text == "ok"
    assert turn.response_metadata == {}


@pytest.mark.asyncio
async def test_qwen_reasoning_probe_uses_effort_with_compatible_cap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    completions = _FakeCompletions()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", lambda **_: _FakeClient(completions))
    provider = openai_provider.OpenAIProvider(
        ProviderConfig(kind="openai_compatible", model="qwen3.7-flash", api_key="test")
    )

    result = await provider._probe_reasoning_control()

    assert result == ("verified", "effort")
    assert completions.calls[0]["max_completion_tokens"] == 16384
    assert completions.calls[0]["reasoning_effort"] == "low"
    assert "extra_body" not in completions.calls[0]


@pytest.mark.asyncio
async def test_generic_reasoning_probe_keeps_openai_effort(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    completions = _FakeCompletions()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", lambda **_: _FakeClient(completions))
    provider = openai_provider.OpenAIProvider(
        ProviderConfig(kind="openai_compatible", model="gpt-5.6-luna", api_key="test")
    )

    result = await provider._probe_reasoning_control()

    assert result == ("verified", "effort")
    assert completions.calls[0]["max_completion_tokens"] == 1024
    assert completions.calls[0]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_vision_probe_uses_high_detail_and_reasoning_safe_budget(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    completions = _FakeCompletions(content="ABCD")
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", lambda **_: _FakeClient(completions))
    provider = openai_provider.OpenAIProvider(
        ProviderConfig(kind="openai_compatible", model="qwen3.7-flash", api_key="test")
    )
    monkeypatch.setattr(provider, "_nonce", lambda _length=6: "ABCD")

    result = await provider._probe_vision()

    assert result == ("verified", None)
    request = completions.calls[0]
    assert request["max_completion_tokens"] == 1024
    image = request["messages"][0]["content"][1]["image_url"]  # type: ignore[index]
    assert image["detail"] == "high"
    assert image["url"].startswith("data:image/png;base64,")
