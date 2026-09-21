"""真实任务入口和工具循环的行为回归；仅替换模型响应与外部 Word 探测。"""

import asyncio
import json

import pytest

from skill_toolbox.agent_loop import ToolDefinition, run_loop
from skill_toolbox.contracts.common import OperationResult
from skill_toolbox.models import (
    AssistantTurn,
    ConversationMessage,
    ToolCall,
)
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest, TaskResult
from skill_toolbox.tools.resume_edit import ResumeEditService


@pytest.fixture(autouse=True)
def no_word_probe(monkeypatch):
    monkeypatch.setattr(
        ResumeEditService,
        "prepare",
        lambda *a, **kw: OperationResult(
            ok=True, data={"header_fields": ["name", "phone", "email"]}
        ),
    )


def turn(name, args=None, ident="call"):
    return AssistantTurn(
        tool_calls=[ToolCall(id=ident, name=name, arguments=args or {})]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "read",
        "write",
        "exec_cmd",
        "spec_append",
        "resume_generate_v2",
        "create_content_plan",
    ],
)
async def test_only_advertised_tools_can_execute(tmp_path, name):
    class Provider(ScriptedProvider):
        async def complete(self, system, messages, tools):
            assert name not in {t["name"] for t in tools}
            if messages[-1].role == "tool":
                result = messages[-1].tool_results[0]
                assert not result.success and "TOOL_NOT_AVAILABLE" in result.content
            return await super().complete(system, messages, tools)

    provider = Provider(
        [
            turn(name, {"path": "unexpected.txt", "content": "must not write"}),
            turn("task_failed", {"error": "probe completed"}),
        ]
    )
    result = await AgentRuntime(provider, lambda e: None).run(
        TaskRequest("resume_pro", "probe", tmp_path / "out")
    )
    assert result.error == "probe completed" and not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_materials_and_multi_tool_results_keep_protocol_pairs(tmp_path):
    source = tmp_path / "facts.txt"
    source.write_text("真实材料：完成自动化测试。", encoding="utf8")

    class Provider:
        step = 0

        async def complete(self, system, messages, tools):
            self.step += 1
            assert "[CAPABILITIES]" in system
            if self.step == 1:
                assert "完成自动化测试" in messages[0].text
                return AssistantTurn(
                    tool_calls=[
                        ToolCall(
                            id="a",
                            name="read_material",
                            arguments={"source_id": "missing"},
                        ),
                        ToolCall(
                            id="b",
                            name="finish_task",
                            arguments={"artifacts": ["fake.docx"]},
                        ),
                    ]
                )
            results = messages[-1].tool_results
            assert [r.tool_call_id for r in results] == ["a", "b"]
            assert not any(r.success for r in results)
            return turn("task_failed", {"error": "done"})

    result = await AgentRuntime(Provider(), lambda e: None).run(
        TaskRequest(
            "resume_pro",
            "制作",
            tmp_path / "out",
            materials=[source],
            capabilities={"vision": False},
        )
    )
    assert result.error == "done"


@pytest.mark.asyncio
async def test_invalid_json_and_schema_do_not_execute_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ResumeEditService, "generate", lambda *a: pytest.fail("无效参数不应启动 Word")
    )
    events = []
    provider = ScriptedProvider(
        [
            turn("resume_generate", {"_invalid_json": "{", "_finish_reason": "length"}),
            turn("resume_generate", {"content": {"sections": []}}),
            turn("task_failed", {"error": "done"}),
        ]
    )
    result = await AgentRuntime(provider, events.append).run(
        TaskRequest("resume_pro", "test", tmp_path)
    )
    assert result.error == "done"
    assert [e["success"] for e in events if e["type"] == "tool_finished"] == [
        False,
        False,
        True,
    ]


@pytest.mark.asyncio
async def test_abort_skips_other_tools_and_legacy_mode_is_retired(tmp_path):
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="a", name="resume_generate"),
                    ToolCall(id="b", name="task_failed", arguments={"error": "stop"}),
                ]
            )
        ]
    )
    events = []
    result = await AgentRuntime(provider, events.append).run(
        TaskRequest("resume_pro", "test", tmp_path)
    )
    assert result.error == "stop"
    assert [e["tool"] for e in events if e["type"] == "tool_started"] == ["task_failed"]
    events.clear()
    retired = await AgentRuntime(ScriptedProvider([]), events.append).run(
        TaskRequest("resume_pro", "test", tmp_path, tool_mode="legacy")
    )
    assert "FEATURE_RETIRED" in retired.error
    assert not any(e["type"] == "model_started" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [False, True])
async def test_text_only_turns_stop_with_bounded_recovery(tmp_path, length):
    response = AssistantTurn(
        text="thinking", response_metadata={"finish_reason": "length"} if length else {}
    )
    events = []
    result = await AgentRuntime(
        ScriptedProvider([response, response]), events.append
    ).run(TaskRequest("resume_pro", "test", tmp_path))
    assert result.status == "failed"
    assert ("输出预算" if length else "未返回工具调用") in result.error
    assert len([e for e in events if e["type"] == "model_started"]) == 2


@pytest.mark.asyncio
async def test_step_limit_and_debug_redaction(tmp_path, monkeypatch):
    from dataclasses import replace
    from skill_toolbox.skills import load_skill

    monkeypatch.setattr(
        "skill_toolbox.runtime.load_skill",
        lambda name: replace(load_skill(name), max_steps=1),
    )
    secret = "sk-leaktest-abcdef123456"
    logs = []
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text=secret,
                tool_calls=[
                    ToolCall(id="x", name="unknown", arguments={"key": secret})
                ],
            )
        ]
    )
    result = await AgentRuntime(provider, lambda e: None, debug_logger=logs.append).run(
        TaskRequest("resume_pro", secret, tmp_path)
    )
    assert "1-step limit" in result.error and secret not in json.dumps(logs)


@pytest.mark.asyncio
async def test_model_timeout_is_reported(tmp_path):
    class Slow:
        async def complete(self, *a):
            await asyncio.Event().wait()

    result = await AgentRuntime(Slow(), lambda e: None, model_timeout_seconds=0.01).run(
        TaskRequest("resume_pro", "test", tmp_path)
    )
    assert "timed out" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_tool_timeout_and_cancellation_call_cleanup(cancelled):
    started = asyncio.Event()

    async def slow(args):
        started.set()
        await asyncio.Event().wait()

    async def stop(args):
        return TaskResult("failed", error="stopped")

    specs = {
        name: ToolDefinition(
            {"name": name, "description": "test", "input_schema": {"type": "object"}},
            fn,
            timeout,
        )
        for name, fn, timeout in [
            ("slow", slow, None if cancelled else 0.01),
            ("task_failed", stop, 1),
        ]
    }
    cleanups, events = [], []
    task = asyncio.create_task(
        run_loop(
            ScriptedProvider([turn("slow"), turn("task_failed")]),
            "",
            [ConversationMessage(role="user", text="go")],
            specs,
            max_steps=3,
            model_timeout=1,
            emit=events.append,
            debug=lambda e: None,
            cancel=cleanups.append,
            transform_context=lambda m: m,
            after_tools=lambda r: None,
        )
    )
    await started.wait()
    if cancelled:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanups == [True]
    else:
        assert (await task).error == "stopped" and cleanups == [False]
        assert any(e["type"] == "tool_timeout" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,count", [("WORD_UNAVAILABLE", 1), ("MEASURE_FAILED", 3)])
async def test_environment_errors_stop_in_domain_policy(
    tmp_path, monkeypatch, code, count
):
    from skill_toolbox.contracts.common import ToolError

    calls = []

    def generate(*args):
        calls.append(1)
        raise ToolError(code, "Word COM failed")

    monkeypatch.setattr(ResumeEditService, "generate", generate)
    content = {
        "content": {
            "sections": [
                {"key": "skills", "title": "技能", "entries": [{"text": ["Python"]}]}
            ]
        }
    }
    provider = ScriptedProvider(
        [turn("resume_generate", content, ident=str(i)) for i in range(5)]
    )
    result = await AgentRuntime(provider, lambda e: None).run(
        TaskRequest("resume_pro", "test", tmp_path)
    )
    assert code in result.error and len(calls) == count
