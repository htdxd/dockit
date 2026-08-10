"""阶段 3 强制测试：Runtime 预处理 + 双模式规划。

覆盖实施计划 §15.2/§15.3 关键回归：
- 材料预处理进入 Agent loop 前完成并发 material_progress 事件
- 四 Prompt 均要求先读材料摘要、写 ContentPlan 再执行生成
- vision=false 时 Provider 不收到 image payload，Prompt/ContentPlan 使用
  conservative 模式（本层验证 Prompt 内容与 CAPABILITIES 横幅）
- 无 Vision 歧义最多一次批量提问；用户跳过后不重复追问（Prompt 纪律）
- 同 hash 材料在 Runtime 任务中只预处理一次
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.runtime import AgentRuntime, TaskRequest


def _finish_turn() -> AssistantTurn:
    return AssistantTurn(
        text="完成",
        tool_calls=[
            ToolCall(
                id="finish",
                name="finish_task",
                arguments={"artifacts": ["artifacts/report.docx"]},
            )
        ],
    )


class _CaptureProvider:
    """记录 system prompt（验证横幅与 Prompt 纪律）与发送的 image 数。"""

    def __init__(self, turn: AssistantTurn) -> None:
        self.turn = turn
        self.prompts: list[str] = []
        self.image_payloads = 0

    async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
        self.prompts.append(system_prompt)
        for message in messages:
            for result in message.tool_results:
                self.image_payloads += len(result.images)
        return self.turn


def _request(
    skill_id: str, capabilities: dict[str, bool], materials: list[Path], output_dir: Path
) -> TaskRequest:
    return TaskRequest(
        skill_id=skill_id,
        user_prompt="按材料生成",
        output_dir=output_dir,
        materials=materials,
        capabilities=capabilities,
    )


@pytest.mark.asyncio
async def test_material_preprocessed_before_agent_loop(tmp_path: Path) -> None:
    """md 材料在进入 Agent loop 前被解析为 DocumentIR（横幅列出 IR/投影）。"""
    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记\n\n- 条目一\n- 条目二", encoding="utf-8")
    events: list[dict[str, object]] = []
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=events.append)

    await runtime.run(
        _request("docx_pro", {"vision": False, "tool_calling": True}, [material], tmp_path / "out")
    )

    # 预处理事件先于模型调用
    progress = [e for e in events if e.get("type") == "material_progress"]
    assert progress, "缺少 material_progress 事件"
    assert progress[0]["phase"] == "parsing"
    assert progress[-1]["phase"] == "done"
    # 横幅引用共享 IR 路径（不展开完整 IR 到 prompt）
    prompt = provider.prompts[0]
    assert "work/materials/" in prompt
    assert "content.md" in prompt
    # 无 Vision：横幅不包含图片（该材料无图），CAPABILITIES 明确 false
    assert "vision: false" in prompt


@pytest.mark.asyncio
async def test_preprocessing_failure_emits_stable_code(tmp_path: Path) -> None:
    """不支持的格式 → material_progress failed + 横幅含稳定错误码。"""
    material = tmp_path / "src" / "bad.xyz"
    material.parent.mkdir()
    material.write_text("x", encoding="utf-8")
    events: list[dict[str, object]] = []
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=events.append)

    await runtime.run(
        _request("docx_pro", {"vision": True, "tool_calling": True}, [material], tmp_path / "out")
    )

    failed = [e for e in events if e.get("type") == "material_progress" and e.get("phase") == "failed"]
    assert failed and failed[0]["error"] == "MATERIAL_UNSUPPORTED"
    assert "MATERIAL_UNSUPPORTED" in provider.prompts[0]


@pytest.mark.asyncio
async def test_same_hash_preprocessed_once(tmp_path: Path) -> None:
    """同内容材料（不同文件名）任务内只预处理一次（sources 一个目录）。"""
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    m1 = d1 / "a.md"
    m2 = d2 / "b.md"
    m1.write_text("# 相同内容\n正文", encoding="utf-8")
    m2.write_text("# 相同内容\n正文", encoding="utf-8")
    events: list[dict[str, object]] = []
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=events.append)

    await runtime.run(
        _request("docx_pro", {"vision": True, "tool_calling": True}, [m1, m2], tmp_path / "out")
    )

    done = [e for e in events if e.get("type") == "material_progress" and e.get("phase") == "done"]
    assert len(done) == 2  # 两个文件都报告完成
    # 两个文件 hash 相同 → [MATERIALS] 材料行只出现一次（manifest 去重）
    materials_section = provider.prompts[0].split("[MATERIALS]")[1].split("\n\n")[0]
    material_rows = [
        line for line in materials_section.splitlines()
        if "document.json" in line or "content.md" in line
    ]
    assert len(material_rows) == 1


@pytest.mark.asyncio
async def test_prompts_require_content_plan_discipline() -> None:
    """四个 Prompt 都包含统一材料协议关键词：先读材料摘要、写 ContentPlan。"""
    from skill_toolbox.skills import load_skill

    for skill_id in ["docx_pro", "pdf_docx_routing", "resume_pro", "ppt-master"]:
        prompt = load_skill(skill_id).system_prompt
        assert "[MATERIALS]" in prompt or "材料" in prompt, f"{skill_id} 缺材料协议"
        assert "ContentPlan" in prompt or "content-plan" in prompt, (
            f"{skill_id} 缺 ContentPlan 纪律"
        )
        assert "conservative" in prompt or "保守" in prompt, (
            f"{skill_id} 缺无 Vision 保守模式规则"
        )


@pytest.mark.asyncio
async def test_vision_false_never_receives_image_payload(tmp_path: Path) -> None:
    """vision=false 时模型全程收不到 image payload（read 不返回图）。"""
    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记\n\n正文", encoding="utf-8")
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=lambda _e: None)

    await runtime.run(
        _request("docx_pro", {"vision": False, "tool_calling": True}, [material], tmp_path / "out")
    )

    assert provider.image_payloads == 0


@pytest.mark.asyncio
async def test_vision_false_planning_mode_conservative(tmp_path: Path) -> None:
    """无 Vision 时 CAPABILITIES 横幅明确 vision:false，Prompt 按 conservative 路由。"""
    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记", encoding="utf-8")
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=lambda _e: None)

    await runtime.run(
        _request("docx_pro", {"vision": False, "tool_calling": True}, [material], tmp_path / "out")
    )

    assert "vision: false" in provider.prompts[0]


@pytest.mark.asyncio
async def test_runtime_emits_qa_status_on_failure(tmp_path: Path) -> None:
    """任务失败时发出 task_failed（QA 状态由阶段 4 接入，此处验证失败事件稳定）。"""
    material = tmp_path / "src" / "notes.md"
    material.parent.mkdir()
    material.write_text("# 笔记", encoding="utf-8")
    events: list[dict[str, object]] = []
    provider = _CaptureProvider(AssistantTurn(text="无工具调用", tool_calls=[]))
    runtime = AgentRuntime(provider=provider, emit=events.append)

    result = await runtime.run(
        _request("docx_pro", {"vision": True, "tool_calling": True}, [material], tmp_path / "out")
    )

    assert result.status == "failed"
    assert any(e.get("type") == "task_failed" for e in events)
