"""End-to-end vision-routing test: the runtime injects a [CAPABILITIES] banner
into the system prompt so prompt-level routing (vision vs non-vision flow)
depends on the resolved capability — not on model self-judgment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.runtime import AgentRuntime, TaskRequest


def _finish_turn() -> AssistantTurn:
    return AssistantTurn(
        text="生成完成",
        tool_calls=[ToolCall(id="c1", name="finish_task", arguments={"artifacts": ["artifacts/report.docx"]})],
    )


class _CaptureProvider:
    def __init__(self) -> None:
        self.prompt: str = ""

    async def complete(self, system_prompt, messages, tools):  # noqa: ANN001
        self.prompt = system_prompt
        return _finish_turn()


def _request(capabilities: dict) -> TaskRequest:
    return TaskRequest(
        skill_id="simple_docx",
        user_prompt="生成测试文档",
        output_dir=Path("_docxpro_e2e_out"),
        capabilities=capabilities,
    )


def test_capabilities_banner_injected_for_vision() -> None:
    provider = _CaptureProvider()
    asyncio.run(AgentRuntime(provider=provider, emit=lambda _event: None).run(
        _request({"vision": True, "tool_calling": True, "json_schema": True})
    ))
    assert "[CAPABILITIES]" in provider.prompt
    assert "vision: true" in provider.prompt


def test_capabilities_banner_injected_for_non_vision() -> None:
    provider = _CaptureProvider()
    asyncio.run(AgentRuntime(provider=provider, emit=lambda _event: None).run(
        _request({"vision": False, "tool_calling": True, "json_schema": True})
    ))
    assert "[CAPABILITIES]" in provider.prompt
    assert "vision: false" in provider.prompt


def test_no_banner_when_no_capabilities() -> None:
    provider = _CaptureProvider()
    asyncio.run(AgentRuntime(provider=provider, emit=lambda _event: None).run(
        _request({})
    ))
    assert "[CAPABILITIES]" not in provider.prompt
