"""阶段 8 强制测试：领域模式 Runtime 切换（实施计划 §8）。

覆盖：
- tool_mode="domain"（TaskRequest 覆盖）时 LLM 只收到领域 schema
  （无 write/edit/exec_cmd/spec_append）
- 领域工具序列 E2E：read_material → create_content_plan → resume_prepare →
  resume_repair → resume_generate → finish_task 成功交付
- 步数在目标范围（docx 4-10 步）；legacy 模式默认不回归（现有契约不变）
- finish 门在领域模式不要求 legacy exec_cmd 质量动作证据（领域 Service 自带机械门）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest
from skill_toolbox.contracts.common import OperationResult


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

_LEGACY_TOOLS = {"write", "edit", "exec_cmd", "spec_append", "read", "ingest"}


class _CaptureProvider:
    """记录每轮收到的 tool schema 名，按脚本返回预设 turn。"""

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.seen_schemas: list[list[str]] = []
        self.image_payloads = 0

    async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
        self.seen_schemas.append([tool["name"] for tool in tools])
        for message in messages:
            for result in message.tool_results:
                self.image_payloads += len(result.images)
        turn = self.turns.pop(0) if self.turns else AssistantTurn(tool_calls=[])
        return turn


def _call(tool_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(id=tool_id, name=name, arguments=arguments)


def _finish(artifacts: list[str]) -> AssistantTurn:
    return AssistantTurn(
        text="交付",
        tool_calls=[_call("finish", "finish_task", {"artifacts": artifacts})],
    )


def test_domain_mode_only_exposes_domain_tools(tmp_path: Path) -> None:
    """领域模式：LLM 只收到 shared + 领域 schema，无底层通用工具。"""
    import asyncio

    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记\n\n正文", encoding="utf-8")
    provider = _CaptureProvider([_finish(["artifacts/x.docx"])])
    runtime = AgentRuntime(provider=provider, emit=lambda _e: None)

    asyncio.run(
        runtime.run(
            TaskRequest(
                "resume_pro",
                "生成文档",
                tmp_path / "out",
                materials=[material],
                capabilities={"vision": False, "tool_calling": True},
                tool_mode="domain",
            )
        )
    )

    assert provider.seen_schemas, "Provider 未收到任何工具 schema"
    for schema_names in provider.seen_schemas:
        assert not (_LEGACY_TOOLS & set(schema_names)), (
            f"领域模式暴露了底层工具: {sorted(_LEGACY_TOOLS & set(schema_names))}"
        )
        assert "resume_prepare" in schema_names
        assert "resume_repair" in schema_names
        assert "resume_generate" in schema_names
        assert "read_material" in schema_names
        assert "create_content_plan" in schema_names
    # 无 Vision：模型不收到图片 payload
    assert provider.image_payloads == 0


def test_legacy_mode_keeps_legacy_tools(tmp_path: Path) -> None:
    """legacy 模式（默认）保留旧工具（渐进迁移不破坏现有接口）。"""
    import asyncio

    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记\n\n正文", encoding="utf-8")
    provider = _CaptureProvider([_finish(["artifacts/x.docx"])])
    runtime = AgentRuntime(provider=provider, emit=lambda _e: None)

    asyncio.run(
        runtime.run(
            TaskRequest(
                "resume_pro",
                "生成文档",
                tmp_path / "out",
                materials=[material],
                capabilities={"vision": False, "tool_calling": True},
            )
        )
    )

    assert provider.seen_schemas
    for schema_names in provider.seen_schemas:
        assert "exec_cmd" in schema_names
        assert "spec_append" in schema_names


def test_domain_mode_artifact_not_fake(tmp_path: Path) -> None:
    """领域模式 finish 仍受 ContentPlan/QA 门约束：无 plan 时不交付。"""
    import asyncio

    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _finish(["artifacts/x.docx"]),
            AssistantTurn(tool_calls=[_call("abort", "task_failed", {"error": "no plan"})]),
        ]
    )
    events: list[dict] = []

    result = asyncio.run(
        AgentRuntime(provider=provider, emit=events.append).run(
            TaskRequest(
                "resume_pro",
                "test",
                tmp_path / "out",
                materials=[material],
                capabilities={"vision": False},
                tool_mode="domain",
            )
        )
    )

    assert result.status == "failed"
    assert not (tmp_path / "out" / "x.resume_pro.docx").exists()
    assert any(
        e.get("type") == "tool_finished"
        and e.get("tool") == "finish_task"
        and e.get("success") is False
        for e in events
    )


def test_operation_result_data_and_domain_artifact_hash_gate(tmp_path: Path) -> None:
    """领域结果需暴露 path；登记后篡改的文件不能绕过 finish 门。"""
    from skill_toolbox.policy import WorkspacePolicy

    assert OperationResult(ok=True, data={"path": "artifacts/a.docx"}).as_dict()["data"]
    artifact = tmp_path / "artifacts" / "a.docx"
    artifact.parent.mkdir()
    artifact.write_text("baseline", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    call = _call("g", "resume_generate", {})
    from skill_toolbox.models import ToolResult

    result = ToolResult(
        tool_call_id="g",
        name="resume_generate",
        success=True,
        content=json.dumps({"ok": True, "data": {"path": "artifacts/a.docx"}}),
    )
    registered: dict[str, str] = {}
    AgentRuntime._record_domain_artifacts([call], [result], policy, registered)
    artifact.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="已被修改"):
        AgentRuntime._verify_domain_artifacts(
            _finish(["artifacts/a.docx"]).tool_calls[0], policy, registered
        )
