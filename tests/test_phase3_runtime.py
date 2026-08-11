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

import json
import os
from pathlib import Path

import pytest
from skill_toolbox.materials import MaterialService
from skill_toolbox.models import AssistantTurn, ToolCall, ToolResult
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.providers.mock import ScriptedProvider
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
    """不支持的格式 → material_progress failed，且不进入 Agent loop。"""
    material = tmp_path / "src" / "bad.xyz"
    material.parent.mkdir()
    material.write_text("x", encoding="utf-8")
    events: list[dict[str, object]] = []
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=events.append)

    result = await runtime.run(
        _request("docx_pro", {"vision": True, "tool_calling": True}, [material], tmp_path / "out")
    )

    failed = [
        e
        for e in events
        if e.get("type") == "material_progress" and e.get("phase") == "failed"
    ]
    assert failed and failed[0]["error"] == "MATERIAL_UNSUPPORTED"
    assert result.status == "failed"
    assert "MATERIAL_UNSUPPORTED" in (result.error or "")
    assert provider.prompts == []


def _empty_content_plan(task_type: str = "docx", mode: str = "conservative") -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "task_type": task_type,
            "mode": mode,
            "selections": [],
            "exclusions": [],
            "questions_asked": False,
        }
    )


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
async def test_same_basename_materials_not_overwritten(tmp_path: Path) -> None:
    """两个不同目录的同名文件暂存后互不覆盖，且都能被预处理（§9.2）。"""
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    m1 = d1 / "same.md"
    m2 = d2 / "same.md"
    m1.write_text("# 第一份\n内容 A", encoding="utf-8")
    m2.write_text("# 第二份\n内容 B", encoding="utf-8")
    events: list[dict[str, object]] = []
    provider = _CaptureProvider(_finish_turn())
    runtime = AgentRuntime(provider=provider, emit=events.append)

    await runtime.run(
        _request("docx_pro", {"vision": True, "tool_calling": True}, [m1, m2], tmp_path / "out")
    )

    # 两文件都已解析（对应两个 material_progress done）
    done = [e for e in events if e.get("type") == "material_progress" and e.get("phase") == "done"]
    assert len(done) == 2
    # 横幅列出两条材料行（内容不同 → 两个不同 hash，未互相覆盖）
    materials_section = provider.prompts[0].split("[MATERIALS]")[1].split("\n\n")[0]
    material_rows = [
        line for line in materials_section.splitlines()
        if "document.json" in line or "content.md" in line
    ]
    assert len(material_rows) == 2


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
async def test_vision_false_read_image_denied(tmp_path: Path) -> None:
    """vision=false 时 read(图片) 被硬性拒绝：无视觉模型收不到 image payload。

    仅依赖 Prompt 自律可能漏发图（实施计划 §3.3 机械保证）；ToolRegistry 按
    capabilities 拒绝图片读，错误明确并指导保守决策。
    """
    from skill_toolbox.models import ToolCall
    from skill_toolbox.policy import WorkspacePolicy
    from skill_toolbox.tools import ToolRegistry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    img = workspace / "fig.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        capabilities={"vision": False, "tool_calling": True},
    )
    result = await registry.execute(
        ToolCall(id="r", name="read", arguments={"path": "fig.png"})
    )
    assert not result.success
    assert "不支持图像理解" in result.content


@pytest.mark.asyncio
async def test_pdf_read_and_ingest_cannot_trigger_second_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skill_toolbox.policy import WorkspacePolicy
    from skill_toolbox.tools import ToolRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.pdf").write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(
        "skill_toolbox.tools.ingest_material",
        lambda *_args, **_kwargs: pytest.fail("PDF ingest called the legacy parser"),
    )
    registry = ToolRegistry(WorkspacePolicy(workspace))

    read_result = await registry.execute(
        ToolCall(id="read", name="read", arguments={"path": "source.pdf"})
    )
    ingest_result = await registry.execute(
        ToolCall(id="ingest", name="ingest", arguments={"path": "source.pdf"})
    )

    assert not read_result.success
    assert not ingest_result.success
    assert "共享材料层" in read_result.content
    assert "禁止重复 MinerU" in ingest_result.content


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


def test_qa_report_schema_and_event_projection(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runtime = AgentRuntime.__new__(AgentRuntime)
    with pytest.raises(ValueError, match="QAReport"):
        runtime._load_qa_report(ws)
    qa_dir = ws / "work" / "qa"
    qa_dir.mkdir(parents=True)
    (qa_dir / "mechanical.json").write_text(
        '{"mechanical": "passed", "visual": "not_run", "used_assets": ["asset-1"], "skipped_assets": []}',
        encoding="utf-8",
    )
    qa = runtime._load_qa_report(ws)
    assert qa.mechanical == "passed"
    assert qa.used_assets == ["asset-1"]
    assert runtime._qa_event(qa)["used_assets"] == 1

    (qa_dir / "mechanical.json").write_text(
        '{"mechanical":"passed","visual":"not_run","used_assets":1}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="QAReport 无效"):
        runtime._load_qa_report(ws)


def test_qa_report_enforces_vision_mode(tmp_path: Path) -> None:
    qa_dir = tmp_path / "work" / "qa"
    qa_dir.mkdir(parents=True)
    report = qa_dir / "report.json"
    report.write_text(
        '{"mechanical":"passed","visual":"not_run"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="vision=true"):
        AgentRuntime._load_qa_report(tmp_path, {"vision": True})

    report.write_text(
        '{"mechanical":"passed","visual":"failed"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="vision=true"):
        AgentRuntime._load_qa_report(tmp_path, {"vision": True})

    report.write_text(
        '{"mechanical":"passed","visual":"passed"}', encoding="utf-8"
    )
    assert AgentRuntime._load_qa_report(tmp_path, {"vision": True}).visual == "passed"


def test_qa_report_tie_break_is_deterministic(tmp_path: Path) -> None:
    qa_dir = tmp_path / "work" / "qa"
    qa_dir.mkdir(parents=True)
    first = qa_dir / "a.json"
    second = qa_dir / "z.json"
    first.write_text(
        '{"mechanical":"passed","visual":"not_run","used_assets":["a"]}',
        encoding="utf-8",
    )
    second.write_text(
        '{"mechanical":"passed","visual":"not_run","used_assets":["z"]}',
        encoding="utf-8",
    )
    timestamp = 1_700_000_000_000_000_000
    os.utime(first, ns=(timestamp, timestamp))
    os.utime(second, ns=(timestamp, timestamp))

    qa = AgentRuntime._load_qa_report(tmp_path)

    assert qa.used_assets == ["z"]


def test_content_plan_validates_mode_and_asset_coverage(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "figure.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    )
    source = source_dir / "notes.md"
    source.write_text("# Notes\n\n![Figure](figure.png)", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = MaterialService(workspace)
    ir = service.prepare_material(source)
    plan_path = workspace / "work" / "plans" / "content-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "task_type": "docx",
                "mode": "conservative",
                "selections": [],
                "exclusions": [
                    {"source_id": ir.assets[0].id, "reason": "irrelevant"}
                ],
                "questions_asked": False,
            }
        ),
        encoding="utf-8",
    )

    plan = AgentRuntime._load_content_plan(
        workspace, "docx", {"vision": False}, service.catalog()
    )
    assert plan.mode == "conservative"
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            '"mode": "conservative"', '"mode": "vision"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conservative"):
        AgentRuntime._load_content_plan(
            workspace, "docx", {"vision": False}, service.catalog()
        )


@pytest.mark.asyncio
async def test_runtime_requires_content_plan_without_materials(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write",
                        arguments={"path": "artifacts/report.md", "content": "内容"},
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="qa",
                        name="write",
                        arguments={
                            "path": "work/qa/report.json",
                            "content": '{"mechanical":"passed","visual":"not_run"}',
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_task",
                        arguments={"artifacts": ["artifacts/report.md"]},
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="abort",
                        name="task_failed",
                        arguments={"error": "missing plan"},
                    )
                ]
            ),
        ]
    )
    events: list[dict[str, object]] = []

    result = await AgentRuntime(provider, events.append).run(
        TaskRequest("docx_pro", "test", tmp_path / "out")
    )

    assert result.status == "failed"
    assert any(
        event.get("type") == "tool_finished"
        and event.get("tool") == "finish_task"
        and event.get("success") is False
        for event in events
    )
    assert any(
        event.get("type") == "qa_status"
        and "ContentPlan" in str(event.get("qa", {}))
        for event in events
    )


@pytest.mark.parametrize(
    ("report", "expected_pass"),
    [
        ({"ok": True, "overflow_risks": []}, True),
        ({"ok": True}, False),
        ({"ok": True, "overflow_risks": None}, False),
        ({"ok": False, "overflow_risks": []}, False),
        ({"ok": True, "overflow_risks": {}}, False),
    ],
)
def test_fill_resume_quality_report_contract(
    tmp_path: Path, report: dict[str, object], expected_pass: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "artifacts" / "resume.docx"
    output.parent.mkdir()
    output.write_bytes(b"docx fixture")
    call = ToolCall(
        id="fill",
        name="exec_cmd",
        arguments={
            "action": "fill_resume",
            "args": {"output": "artifacts/resume.docx"},
        },
    )
    result = ToolResult(
        tool_call_id="fill",
        name="exec_cmd",
        success=True,
        content=json.dumps({"stdout": json.dumps(report)}),
    )
    passed: set[str] = set()
    hashes: dict[str, set[str]] = {}

    AgentRuntime._record_quality_results(
        [call],
        [result],
        frozenset({"fill_resume"}),
        passed,
        hashes,
        WorkspacePolicy(workspace),
    )

    assert ("fill_resume" in passed) is expected_pass

@pytest.mark.asyncio
async def test_agent_written_qa_cannot_bypass_mechanical_action(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="plan",
                        name="write",
                        arguments={
                            "path": "work/plans/content-plan.json",
                            "content": _empty_content_plan(),
                        },
                    )
                ]
            ),
            AssistantTurn(tool_calls=[ToolCall(
                id="a", name="write", arguments={"path": "artifacts/fake.docx", "content": "fake"}
            )]),
            AssistantTurn(tool_calls=[ToolCall(
                id="q", name="write", arguments={
                    "path": "work/qa/mechanical.json",
                    "content": '{"mechanical":"passed","visual":"not_run"}',
                }
            )]),
            AssistantTurn(tool_calls=[ToolCall(
                id="f", name="finish_task", arguments={"artifacts": ["artifacts/fake.docx"]}
            )]),
            AssistantTurn(tool_calls=[ToolCall(
                id="x", name="task_failed", arguments={"error": "mechanical gate rejected"}
            )]),
        ]
    )
    events: list[dict[str, object]] = []
    result = await AgentRuntime(provider, events.append).run(
        TaskRequest("docx_pro", "test", tmp_path / "out")
    )

    assert result.status == "failed"
    assert not (tmp_path / "out" / "fake.docx_pro.docx").exists()
    assert any(
        event.get("type") == "tool_finished"
        and event.get("tool") == "finish_task"
        and event.get("success") is False
        for event in events
    )


def _docx_spec(title: str) -> str:
    return json.dumps(
        {
            "title": title,
            "complexity": "simple",
            "sections": [
                {
                    "heading": "正文",
                    "level": 1,
                    "paragraphs": [{"text": f"{title} content"}],
                }
            ],
        },
        ensure_ascii=False,
    )


def _quality_hash_provider(deliver: str, *, mutate_checked: bool) -> ScriptedProvider:
    turns = [
        AssistantTurn(
            tool_calls=[
                ToolCall(
                    id="plan",
                    name="write",
                    arguments={
                        "path": "work/plans/content-plan.json",
                        "content": _empty_content_plan(),
                    },
                )
            ]
        ),
        AssistantTurn(
            tool_calls=[
                ToolCall(
                    id="spec-a",
                    name="write",
                    arguments={"path": "work/a.json", "content": _docx_spec("A")},
                ),
                ToolCall(
                    id="spec-b",
                    name="write",
                    arguments={"path": "work/b.json", "content": _docx_spec("B")},
                ),
            ]
        ),
        AssistantTurn(
            tool_calls=[
                ToolCall(
                    id="build-a",
                    name="exec_cmd",
                    arguments={
                        "action": "build_docx",
                        "args": {"source": "work/a.json", "output": "artifacts/a.docx"},
                    },
                ),
                ToolCall(
                    id="build-b",
                    name="exec_cmd",
                    arguments={
                        "action": "build_docx",
                        "args": {"source": "work/b.json", "output": "artifacts/b.docx"},
                    },
                ),
            ]
        ),
        AssistantTurn(
            tool_calls=[
                ToolCall(
                    id="check-a",
                    name="exec_cmd",
                    arguments={
                        "action": "postcheck_docx",
                        "args": {"source": "artifacts/a.docx"},
                    },
                )
            ]
        ),
    ]
    if mutate_checked:
        turns.append(
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="mutate-a",
                        name="exec_cmd",
                        arguments={
                            "action": "build_docx",
                            "args": {
                                "source": "work/b.json",
                                "output": "artifacts/a.docx",
                            },
                        },
                    )
                ]
            )
        )
    turns.extend(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="qa",
                        name="write",
                        arguments={
                            "path": "work/qa/report.json",
                            "content": '{"mechanical":"passed","visual":"not_run"}',
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_task",
                        arguments={"artifacts": [deliver]},
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="abort",
                        name="task_failed",
                        arguments={"error": "quality hash gate rejected delivery"},
                    )
                ]
            ),
        ]
    )
    return ScriptedProvider(turns)


@pytest.mark.asyncio
async def test_quality_action_for_a_cannot_publish_b(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    result = await AgentRuntime(
        _quality_hash_provider("artifacts/b.docx", mutate_checked=False), events.append
    ).run(
        TaskRequest(
            "docx_pro",
            "test",
            tmp_path / "out",
            capabilities={"vision": False},
        )
    )

    assert result.status == "failed"
    assert not (tmp_path / "out" / "b.docx_pro.docx").exists()
    assert any(
        event.get("tool") == "finish_task" and event.get("success") is False
        for event in events
    )


@pytest.mark.asyncio
async def test_checked_docx_modified_after_action_is_rejected(tmp_path: Path) -> None:
    result = await AgentRuntime(
        _quality_hash_provider("artifacts/a.docx", mutate_checked=True),
        lambda _event: None,
    ).run(
        TaskRequest(
            "docx_pro",
            "test",
            tmp_path / "out",
            capabilities={"vision": False},
        )
    )

    assert result.status == "failed"
    assert not (tmp_path / "out" / "a.docx_pro.docx").exists()
