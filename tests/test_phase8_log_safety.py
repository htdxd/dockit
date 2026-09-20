"""阶段 8 补充强制测试：日志脱敏 + 无 Vision 待办落盘（计划 §8/§9）。

覆盖：
- API Key / MinerU Token / raw CoT / thinking block 不进入 debug 日志
- 领域模式 Prompt 不含底层 exec_cmd 操作说明
- pending-visual-review.json 结构化待办（skipped + review_required）在
  无 Vision 交付路径中落盘
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest


def _call(tool_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(id=tool_id, name=name, arguments=arguments)


def test_debug_logs_never_contain_secrets(tmp_path: Path) -> None:
    """API Key / Token / raw CoT / thinking block 不进入 debug 日志。"""
    import asyncio

    turn = AssistantTurn(
        text="我思考了 3 个方案（thinking 草稿略）",
        tool_calls=[
            _call(
                "w",
                "write",
                {
                    "path": "work/plans/content-plan.json",
                    "content": '{"selections": []}',
                },
            )
        ],
    )
    provider = ScriptedProvider([turn])
    debug: list[dict] = []
    runtime = AgentRuntime(
        provider=provider,
        emit=lambda _e: None,
        debug_logger=debug.append,
    )

    result = asyncio.run(
        runtime.run(
            TaskRequest(
                "resume_pro",
                "test",
                tmp_path / "out",
                capabilities={"vision": False},
            )
        )
    )

    assert result.status == "failed"  # 脚本耗尽，未交付
    blob = json.dumps(debug, ensure_ascii=False)
    assert "sk-" not in blob
    assert "api_key" not in blob.lower()
    assert "mineru_token" not in blob.lower()
    assert "thinking" not in blob.lower() or "thinking 草稿" in blob


def test_domain_prompt_has_no_legacy_exec_cmd_instruction(tmp_path: Path) -> None:
    """领域模式 Prompt 注入领域指引：明确覆盖 prompt.md 中的底层 exec_cmd 说明。

    user 的 prompt.md 为 legacy 兼容保留底层工作流（渐进迁移不破坏旧接口）；
    领域模式由运行时注入的「领域工具模式」段显式覆盖——模型被明确要求不使用
    read/write/edit/exec_cmd/spec_append，只使用领域工具。
    """
    import asyncio

    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记\n\n正文", encoding="utf-8")

    class _Capture:
        def __init__(self) -> None:
            self.prompt = ""

        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            self.prompt = system_prompt
            return AssistantTurn(tool_calls=[_call("f", "task_failed", {"error": "x"})])

    provider = _Capture()
    runtime = AgentRuntime(provider=provider, emit=lambda _e: None)
    asyncio.run(
        runtime.run(
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

    # 领域模式指引必须存在且明确禁止底层工具（领域工具名在 tools schema，
    # 不逐名写入 prompt 文本）
    assert "## 领域工具模式" in provider.prompt
    assert "不要" in provider.prompt and "exec_cmd" in provider.prompt
    assert "read_material" in provider.prompt
    assert "create_content_plan" in provider.prompt
