"""阶段 9 强制测试：历史日志失败模式回归（实施计划 §9/§12）。

计划 §2.1 观察到的历史失败模式，每条至少一个回归测试（领域模式与 legacy
兼容路径双覆盖）：

1. 占位符泄漏：未渲染占位符（{pages}/{name}）不得进入脚本/产物
2. 图片路径自造：模型自造 asset_id/路径被当场拒绝
3. old_text 不匹配：legacy edit 明确报错（不给循环修复机会）
4. JSON 损坏：损坏 JSON 写入被拒绝（不进入半成品）
5. block 超限：spec_append / DocxBlock 超 8 块被拒
6. 错误 action：legacy exec_cmd 白名单外 action 被拒
7. 达到步数上限：领域模式耗尽步骤以 task_failed 收尾（不伪造交付）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skill_toolbox.models import AssistantTurn, ToolCall, ToolResult
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest
from skill_toolbox.tools import ToolRegistry

_LEGACY_SPECS = {
    "echo": ("echo.py", ()),
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _registry(workspace: Path) -> ToolRegistry:
    return ToolRegistry(WorkspacePolicy(workspace), scripts=_LEGACY_SPECS)


# ---------------- 1. 占位符泄漏 ----------------

def test_unrendered_placeholder_rejected(workspace: Path) -> None:
    """legacy exec_cmd 缺参数：未渲染占位符被当场拒绝（不传给脚本）。"""
    import subprocess
    import sys

    skill_dir = workspace / "skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "echo.py").write_text(
        "import sys\nprint(sys.argv)\n", encoding="utf-8"
    )
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        skill_dir=skill_dir,
        scripts={"echo": ("echo.py", ("{pages}",))},
    )
    result = asyncio.run(
        registry.execute(
            ToolCall(id="e", name="exec_cmd", arguments={"action": "echo", "args": {}})
        )
    )
    assert not result.success
    assert "未渲染的占位符" in result.content or "缺少必要参数" in result.content


# ---------------- 2. 图片路径自造 ----------------

def test_domain_rejects_invented_asset_id(workspace: Path) -> None:
    from skill_toolbox.materials import MaterialService
    from skill_toolbox.tools.materials import MaterialPlanService
    from skill_toolbox.contracts.common import ToolError
    service = MaterialPlanService(workspace, MaterialService(workspace).catalog(), "resume")
    with pytest.raises(ToolError) as exc:
        service.read_material("asset-invented-abc")
    assert exc.value.code == "MATERIAL_UNKNOWN_ID"


def test_legacy_edit_old_text_not_found(workspace: Path) -> None:
    """legacy edit：old_text 不匹配明确报错，模型可一次纠正。"""
    target = workspace / "work" / "a.json"
    target.parent.mkdir()
    target.write_text('{"a": 1}', encoding="utf-8")
    result = asyncio.run(
        _registry(workspace).execute(
            ToolCall(
                id="e",
                name="edit",
                arguments={"path": "work/a.json", "old_text": "不存在", "new_text": "x"},
            )
        )
    )
    assert not result.success
    assert "old_text was not found" in result.content


# ---------------- 4. JSON 损坏 ----------------

def test_legacy_edit_rejects_broken_json(workspace: Path) -> None:
    """legacy edit 会破坏 JSON 结构时拒绝写入（不留半成品）。"""
    target = workspace / "work" / "b.json"
    target.parent.mkdir()
    target.write_text('{"a": 1}', encoding="utf-8")
    result = asyncio.run(
        _registry(workspace).execute(
            ToolCall(
                id="e",
                name="edit",
                arguments={"path": "work/b.json", "old_text": "1", "new_text": "[broken"},
            )
        )
    )
    assert not result.success
    assert "会破坏 JSON 结构" in result.content
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


# ---------------- 5. block 超限 ----------------

def test_legacy_spec_append_block_limit(workspace: Path) -> None:
    """legacy spec_append：一次超过 8 个 block 被拒（防大 JSON 截断）。"""
    result = asyncio.run(
        _registry(workspace).execute(
            ToolCall(
                id="s",
                name="spec_append",
                arguments={
                    "spec": "work/spec.json",
                    "blocks": [{"type": "paragraph", "text": f"p{i}"} for i in range(9)],
                },
            )
        )
    )
    assert not result.success
    assert "一次最多" in result.content


# ---------------- 6. 错误 action ----------------

def test_legacy_unknown_action_rejected(workspace: Path) -> None:
    """legacy exec_cmd：白名单外 action 明确拒绝（"Action is not allowed"）。"""
    result = asyncio.run(
        _registry(workspace).execute(
            ToolCall(id="x", name="exec_cmd", arguments={"action": "list_templates", "args": {}})
        )
    )
    assert not result.success
    assert "Action is not allowed" in result.content


# ---------------- 7. 步数上限（领域模式） ----------------

def test_domain_mode_step_limit_fails_cleanly(tmp_path: Path) -> None:
    """领域模式耗尽步骤：以 task_failed 收尾，不伪造交付。"""
    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记", encoding="utf-8")
    turns = [
        AssistantTurn(
            tool_calls=[ToolCall(id="r", name="read_material", arguments={"source_id": "block-x"})]
        )
        for _ in range(24)
    ]
    events: list[dict] = []
    result = asyncio.run(
        AgentRuntime(ScriptedProvider(turns), events.append).run(
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
    assert "step limit" in (result.error or "") or "step" in (result.error or "").lower()
    assert not list((tmp_path / "out").glob("*.docx*"))
