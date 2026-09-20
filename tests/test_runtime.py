import asyncio
import dataclasses
from pathlib import Path

import pytest
from skill_toolbox.models import AssistantTurn, ToolCall, ToolResult
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest
from skill_toolbox.skills import load_skill

EMPTY_DOCX_PLAN = (
    '{"schema_version":"1","task_type":"resume","mode":"conservative",'
    '"selections":[],"exclusions":[],"questions_asked":false}'
)


def _disable_skill_quality_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skill_toolbox.runtime.load_skill",
        lambda skill_id: dataclasses.replace(
            load_skill(skill_id), quality_actions=frozenset()
        ),
    )


class HangingProvider:
    async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
        del system_prompt, messages, tools
        await asyncio.sleep(1)
        return AssistantTurn()


class HangingTools:
    async def execute(self, call: ToolCall) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult(
            tool_call_id=call.id, name=call.name, success=True, content="late"
        )


class LargeResultTools:
    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            success=True,
            content="x" * 200_000,
        )


@pytest.mark.asyncio
async def test_runtime_publishes_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_skill_quality_gate(monkeypatch)
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="write",
                        arguments={
                            "path": "work/plans/content-plan.json",
                            "content": EMPTY_DOCX_PLAN,
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write-spec",
                        name="write",
                        arguments={
                            "path": "artifacts/runtime-test.md",
                            "content": "Agent loop 已成功生成产物。",
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write-qa",
                        name="write",
                        arguments={
                            "path": "work/qa/mechanical.json",
                            "content": '{"mechanical": "passed", "visual": "not_run"}',
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_task",
                        arguments={"artifacts": ["artifacts/runtime-test.md"]},
                    )
                ]
            ),
        ]
    )
    events: list[dict[str, object]] = []
    runtime = AgentRuntime(provider=provider, emit=events.append)

    result = await runtime.run(
        TaskRequest(
            skill_id="resume_pro",
            user_prompt="生成测试文档",
            output_dir=tmp_path / "published",
        )
    )

    assert result.status == "completed"
    assert result.artifacts == [tmp_path / "published" / "runtime-test.resume_pro.md"]
    assert result.artifacts[0].exists()
    assert any(event["type"] == "tool_started" for event in events)
    assert {
        "type": "tool_finished",
        "tool": "finish_task",
        "success": True,
    } in events
    assert events[-1]["type"] == "task_completed"


@pytest.mark.asyncio
async def test_finish_task_must_be_the_only_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_skill_quality_gate(monkeypatch)
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="write",
                        arguments={
                            "path": "work/plans/content-plan.json",
                            "content": EMPTY_DOCX_PLAN,
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write",
                        arguments={"path": "artifacts/mixed.md", "content": "测试"},
                    ),
                    ToolCall(
                        id="early-finish",
                        name="finish_task",
                        arguments={"artifacts": ["artifacts/mixed.md"]},
                    ),
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write-qa",
                        name="write",
                        arguments={
                            "path": "work/qa/mechanical.json",
                            "content": '{"mechanical": "passed", "visual": "not_run"}',
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_task",
                        arguments={"artifacts": ["artifacts/mixed.md"]},
                    )
                ]
            ),
        ]
    )
    runtime = AgentRuntime(provider=provider, emit=lambda _: None)

    result = await runtime.run(TaskRequest("resume_pro", "测试", tmp_path))

    assert result.status == "completed"
    assert result.artifacts[0].exists()


@pytest.mark.asyncio
async def test_runtime_fails_with_clear_error_when_model_times_out(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    runtime = AgentRuntime(
        provider=HangingProvider(),
        emit=events.append,
        model_timeout_seconds=0.01,
    )

    result = await runtime.run(TaskRequest("resume_pro", "测试", tmp_path))

    assert result.status == "failed"
    assert result.error == "LLM request timed out after 0.01 seconds"
    assert events[-1] == {
        "type": "task_failed",
        "error": "LLM request timed out after 0.01 seconds",
    }


@pytest.mark.asyncio
async def test_tool_timeout_returns_failure_and_emits_timeout() -> None:
    events: list[dict[str, object]] = []
    runtime = AgentRuntime(
        provider=HangingProvider(),
        emit=events.append,
        tool_timeout_seconds=0.01,
    )
    call = ToolCall(id="slow", name="write", arguments={"content": "data"})

    result = await runtime._execute_call(call, HangingTools())  # type: ignore[arg-type]

    assert result.success is False
    assert result.content == "Tool timed out after 0.01 seconds"
    assert events == [
        {"type": "tool_started", "tool": "write"},
        {"type": "tool_timeout", "tool": "write", "timeout_seconds": 0.01},
    ]


@pytest.mark.asyncio
async def test_tool_progress_events_do_not_echo_large_payloads() -> None:
    events: list[dict[str, object]] = []
    runtime = AgentRuntime(provider=HangingProvider(), emit=events.append)
    call = ToolCall(
        id="large",
        name="write",
        arguments={"path": "work/data.txt", "content": "x" * 200_000},
    )

    result = await runtime._execute_call(call, LargeResultTools())  # type: ignore[arg-type]

    assert result.success is True
    assert events == [
        {"type": "tool_started", "tool": "write"},
        {"type": "tool_finished", "tool": "write", "success": True},
    ]
