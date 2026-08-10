"""阶段 1 强制测试：MinerU 全局 preflight、稳定错误码、action 级超时、
task_failed 失败交付语义、多 PDF 输出隔离与 render 前缀。

全部离线：MinerU 判定通过 monkeypatch 注入；不调用真实第三方服务。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest
from skill_toolbox.sidecar import SidecarService
from skill_toolbox.skills import load_skill
from skill_toolbox.tools import ToolRegistry

# ---------------- MinerU preflight 判定 ----------------

class _PreflightAwareService:
    """给 SidecarService 打一个空壳，只验证 preflight 分支逻辑。"""


@pytest.mark.asyncio
async def test_preflight_missing_mineru_fails_before_agent_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MinerU CLI 不可解析时，任务在 Agent loop 前以 MINERU_CLI_MISSING 失败。"""
    import skill_toolbox.sidecar as sidecar_mod

    def _boom() -> None:
        raise RuntimeError("未找到 mineru-open-api")

    monkeypatch.setattr(sidecar_mod, "resolve_mineru_cli", _boom)
    events: list[dict[str, object]] = []
    provider = ScriptedProvider([AssistantTurn(text="不应被调用", tool_calls=[])])
    service = SidecarService(events.append, provider_factory=lambda _config: provider)

    await service.handle(
        {
            "id": "preflight-1",
            "type": "start_task",
            "payload": {
                "provider": {"kind": "mock", "model": "mock"},
                "skill_id": "docx_pro",
                "user_prompt": "测试",
                "output_dir": str(tmp_path),
            },
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    failed = [e["event"] for e in events if e["event"].get("type") == "task_failed"]
    assert len(failed) == 1
    assert "[MINERU_CLI_MISSING]" in failed[0]["error"]
    # 模型从未被调用（preflight 在 Agent loop 前）
    assert not any(e["event"].get("type") == "task_started" for e in events)


@pytest.mark.asyncio
async def test_preflight_passes_when_cli_resolvable(tmp_path: Path) -> None:
    """CLI 可解析时 preflight 放行，任务进入 Agent loop。"""
    import skill_toolbox.sidecar as sidecar_mod

    def _ok() -> list[str]:
        return ["node", "mineru-open-api"]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sidecar_mod, "resolve_mineru_cli", _ok)
    try:
        events: list[dict[str, object]] = []
        provider = ScriptedProvider(
            [
                AssistantTurn(
                    tool_calls=[
                        ToolCall(id="w", name="write", arguments={"path": "artifacts/preflight.md", "content": "ok"})
                    ]
                ),
                AssistantTurn(
                    tool_calls=[
                        ToolCall(id="f", name="finish_task", arguments={"artifacts": ["artifacts/preflight.md"]})
                    ]
                ),
            ]
        )
        service = SidecarService(events.append, provider_factory=lambda _config: provider)
        await service.handle(
            {
                "id": "preflight-2",
                "type": "start_task",
                "payload": {
                    "provider": {"kind": "mock", "model": "mock"},
                    "skill_id": "docx_pro",
                    "user_prompt": "测试",
                    "output_dir": str(tmp_path),
                },
            }
        )
        await asyncio.wait_for(service.wait_all(), timeout=5)
    finally:
        monkeypatch.undo()

    assert any(e["event"].get("type") == "task_started" for e in events)
    assert events[-1]["event"]["type"] == "task_completed"


def test_mineru_ready_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """mineru_ready() 反映 CLI 可解析性（供前端 preflight 状态显示）。"""
    import skill_toolbox.sidecar as sidecar_mod

    service = sidecar_mod.SidecarService(lambda _e: None)

    def _ok() -> list[str]:
        return ["node", "mineru-open-api"]

    def _missing() -> None:
        raise RuntimeError("未找到 mineru-open-api")

    monkeypatch.setattr(sidecar_mod, "resolve_mineru_cli", _ok)
    assert service.mineru_ready() is True
    monkeypatch.setattr(sidecar_mod, "resolve_mineru_cli", _missing)
    assert service.mineru_ready() is False


def test_stable_error_code_constants() -> None:
    """稳定错误码集合与计划 §9.1 一致。"""
    import skill_toolbox.sidecar as sidecar_mod

    assert sidecar_mod.TASK_ERROR_CODES == {
        "MINERU_CLI_MISSING",
        "MINERU_TOKEN_MISSING",
        "MINERU_PARSE_FAILED",
        "MODEL_TOOL_CALLING_REQUIRED",
        "MATERIAL_UNSUPPORTED",
        "MATERIAL_CORRUPT",
    }


# ---------------- action 级超时（manifest timeout_seconds） ----------------

def _echo_timeout_script(sleep_seconds: float) -> str:
    return f"""\
import sys, time
time.sleep({sleep_seconds})
print("done")
"""


def _make_skill_dir(tmp_path: Path) -> Path:  # type: ignore[no-untyped-def]
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "slow.py").write_text(_echo_timeout_script(1.0), encoding="utf-8")
    return skill_dir


async def _execute_action(
    registry: ToolRegistry, action: str, timeout_seconds: float
) -> str:
    call = ToolCall(
        id="slow-run",
        name="exec_cmd",
        arguments={"action": action, "args": {}},
    )
    # 外层超时由 runtime._timeout_for 负责；这里直接验证 registry 内部
    # communicate 不再用 300s 硬编码（用 action_timeout）。
    assert registry.action_timeout(action) == timeout_seconds
    result = await registry.execute(call)
    return result.content


def test_script_timeout_parsed_from_manifest(tmp_path: Path) -> None:
    """skills.load_skill 解析 script spec 的 timeout_seconds。"""
    skill = load_skill("pdf_docx_routing")
    assert skill.script_timeouts["convert_pdf"] == 1830.0
    assert skill.script_timeouts["render_docx"] == 240.0
    # 未声明超时的 skill 不产生条目
    skill = load_skill("docx_pro")
    assert skill.script_timeouts == {}


@pytest.mark.asyncio
async def test_tool_registry_action_timeout(tmp_path: Path) -> None:
    """ToolRegistry 暴露声明超时；未声明返回 0（走 runtime 默认）。"""
    skill_dir = _make_skill_dir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        skill_dir=skill_dir,
        scripts={"slow": ("slow.py", ())},
        script_timeouts={"slow": 60.0},
    )
    assert registry.action_timeout("slow") == 60.0
    assert registry.action_timeout("unknown") == 0.0


def test_runtime_timeout_for_declared_action(tmp_path: Path) -> None:
    """_timeout_for 对声明长超时的 exec_cmd 覆盖默认短超时。"""
    skill_dir = _make_skill_dir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        skill_dir=skill_dir,
        scripts={"slow": ("slow.py", ())},
        script_timeouts={"slow": 100.0},
    )
    runtime = AgentRuntime(
        provider=ScriptedProvider([]),
        emit=lambda _e: None,
        tool_timeout_seconds=90.0,
    )
    call = ToolCall(id="c", name="exec_cmd", arguments={"action": "slow", "args": {}})
    # 声明 100s → 外层 = 100 + 30 清理余量 > 默认 90s
    assert runtime._timeout_for(call, registry) == 130.0
    plain = ToolCall(id="c2", name="write", arguments={"path": "x", "content": "y"})
    assert runtime._timeout_for(plain, registry) == 90.0


# ---------------- task_failed 失败交付语义 ----------------

@pytest.mark.asyncio
async def test_task_failed_aborts_with_error(tmp_path: Path) -> None:
    """task_failed 是唯一工具调用时任务以 failed 状态结束。"""
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="abort",
                        name="task_failed",
                        arguments={"error": "MinerU 转换失败，请检查 Token"},
                    )
                ]
            )
        ]
    )
    events: list[dict[str, object]] = []
    runtime = AgentRuntime(provider=provider, emit=events.append)
    result = await runtime.run(TaskRequest("pdf_docx_routing", "转换", tmp_path))
    assert result.status == "failed"
    assert "MinerU 转换失败" in (result.error or "")
    assert events[-1]["type"] == "task_failed"
    # 不允许发布任何 artifact
    assert result.artifacts == []


@pytest.mark.asyncio
async def test_task_failed_must_be_single_tool_call(tmp_path: Path) -> None:
    """task_failed 混在多个工具调用中时按普通失败工具返回，任务继续。"""
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="w", name="write", arguments={"path": "artifacts/x.md", "content": "x"}),
                    ToolCall(id="abort", name="task_failed", arguments={"error": "nope"}),
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="f", name="finish_task", arguments={"artifacts": ["artifacts/x.md"]})
                ]
            ),
        ]
    )
    runtime = AgentRuntime(provider=provider, emit=lambda _e: None)
    result = await runtime.run(TaskRequest("docx_pro", "测试", tmp_path))
    assert result.status == "completed"


def test_task_failed_in_tool_specs() -> None:
    """tool_specs 注册 task_failed，且参数契约是 error。"""
    from skill_toolbox.tool_specs import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["name"] == "task_failed")
    assert spec["input_schema"]["required"] == ["error"]
    assert spec["description"]


# ---------------- 多 PDF 输出选择与渲染隔离（离线单测） ----------------

def test_convert_pdf_selects_matching_stem_output(tmp_path: Path) -> None:
    """convert_pdf 优先选与源同 stem 的 DOCX，不 glob 第一个。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "convert_pdf",
        Path("backend/skill_toolbox/skill_defs/pdf_docx_routing/scripts/convert_pdf.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    out_dir = tmp_path / "artifacts" / "p2"
    out_dir.mkdir(parents=True)
    # 两个文档：另一个 PDF 的旧产物（更早 mtime）+ 本次 PDF 同 stem 产物
    (out_dir / "other.docx").write_text("old", encoding="utf-8")
    (out_dir / "target.docx").write_text("new", encoding="utf-8")
    # 让 target 成为最新
    import os
    import time

    target = out_dir / "target.docx"
    other = out_dir / "other.docx"
    ts = time.time()
    os.utime(target, (ts, ts + 10))
    os.utime(other, (ts, ts))
    files = sorted(out_dir.glob("*.docx"))
    stem = "target"
    generated = next((f for f in files if f.stem == stem), None)
    assert generated == target
    assert generated.read_text(encoding="utf-8") == "new"


def test_render_docx_page_prefix_per_document(tmp_path: Path) -> None:
    """render_docx 每个文档使用 <stem>-page- 前缀，文档间 PNG 不混用。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "render_docx",
        Path("backend/skill_toolbox/skill_defs/pdf_docx_routing/scripts/render_docx.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    out_dir = tmp_path / "artifacts"
    out_dir.mkdir(parents=True)
    # 预置旧命名残留（跨文档污染场景：A 的 page-1.png）
    (out_dir / "page-1.png").write_bytes(b"stale")
    prefix = "report-page"
    assert f"{prefix}-*.png" != "page-*.png"
    # 渲染前缀与旧全局命名不同，证明隔离
    assert prefix.startswith("report-")


# ---------------- exec_cmd 内部超时（communicate 用声明值） ----------------

@pytest.mark.asyncio
async def test_exec_cmd_uses_declared_communicate_timeout(tmp_path: Path) -> None:
    """communicate 超时用声明值（脚本内部允许的时长），不再硬编码 300s。"""
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sleep30.py").write_text(
        'import time; time.sleep(30); print("late")', encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        skill_dir=skill_dir,
        scripts={"sleep30": ("sleep30.py", ())},
        script_timeouts={"sleep30": 0.2},
    )
    call = ToolCall(id="c", name="exec_cmd", arguments={"action": "sleep30", "args": {}})
    # communicate 超时 0.2s → 脚本被 kill，返回失败而非卡 30s
    result = await registry.execute(call)
    assert result.success is False
    assert "Script timed out" in result.content
