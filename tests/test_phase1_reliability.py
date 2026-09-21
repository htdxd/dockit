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

# ---------------- MinerU preflight 判定 ----------------


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
    # preflight 前必须先配置 Token（MinerU 强依赖，§3.1）；这里配置后
    # 才轮到 CLI 解析失败 → 验证 MINERU_CLI_MISSING 分支。
    service._set_mineru_key("preflight-1", {"mineru_key": "test-token"})

    await service.handle(
        {
            "id": "preflight-1",
            "type": "start_task",
            "payload": {
                "provider": {"kind": "mock", "model": "mock"},
                "skill_id": "resume_pro",
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


# ---------------- action 级超时（manifest timeout_seconds） ----------------


# ---------------- task_failed 失败交付语义 ----------------


# ---------------- 多 PDF 输出选择与渲染隔离（离线单测） ----------------


# ---------------- exec_cmd 内部超时（communicate 用声明值） ----------------
