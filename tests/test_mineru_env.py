import asyncio

import pytest
from skill_toolbox.models import ToolCall
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.sidecar import SidecarService
from skill_toolbox.tools import ToolRegistry

ECHO_ENV_SCRIPT = """\
import os, sys
print(os.environ.get("MINERU_TOKEN", "MISSING"))
sys.exit(0)
"""


def _make_skill_dir(tmp_path: Path) -> Path:  # type: ignore[no-untyped-def]
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "echo_env.py").write_text(ECHO_ENV_SCRIPT, encoding="utf-8")
    return skill_dir


async def _run_exec_cmd(tool_registry: ToolRegistry, tmp_path: Path) -> str:  # type: ignore[no-untyped-def]
    call = ToolCall(
        id="run-env",
        name="exec_cmd",
        arguments={"action": "echo_env", "args": {}},
    )
    result = await tool_registry.execute(call)
    assert result.success, result.content
    return result.content


@pytest.mark.asyncio
async def test_exec_cmd_receives_extra_env(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """ToolRegistry forwards extra_env to exec_cmd subprocesses (MINERU_TOKEN)."""
    skill_dir = _make_skill_dir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        skill_dir=skill_dir,
        scripts={"echo_env": ("echo_env.py", ())},
        env={"MINERU_TOKEN": "sk-mineru-test"},
    )
    content = await _run_exec_cmd(registry, tmp_path)
    assert "sk-mineru-test" in content


@pytest.mark.asyncio
async def test_exec_cmd_omits_extra_env_when_unset(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Without extra_env, MINERU_TOKEN is absent from the subprocess env."""
    skill_dir = _make_skill_dir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry(
        WorkspacePolicy(workspace),
        skill_dir=skill_dir,
        scripts={"echo_env": ("echo_env.py", ())},
    )
    content = await _run_exec_cmd(registry, tmp_path)
    assert "MISSING" in content


def test_task_env_routes_mineru_token_to_pdf_skill_only() -> None:
    """The token reaches pdf_docx_routing subprocesses but never other skills."""
    service = SidecarService(lambda _event: None)
    service._mineru_key = "sk-mineru"  # type: ignore[attr-defined]
    assert service._task_env("pdf_docx_routing") == {"MINERU_TOKEN": "sk-mineru"}
    assert service._task_env("docx_pro") == {}
    assert service._task_env("ppt-master") == {}


@pytest.mark.asyncio
async def test_set_mineru_key_updates_and_clears() -> None:
    """set_mineru_key stores the token; clearing resets it (key never persisted)."""
    events: list[dict[str, object]] = []
    service = SidecarService(events.append)
    await service.handle(
        {"id": "m1", "type": "set_mineru_key", "payload": {"mineru_key": "  sk-123  "}}
    )
    assert service._mineru_key == "sk-123"  # type: ignore[attr-defined]
    assert service._task_env("pdf_docx_routing") == {"MINERU_TOKEN": "sk-123"}
    await service.handle({"id": "m2", "type": "clear_mineru_key", "payload": {}})
    assert service._mineru_key is None  # type: ignore[attr-defined]
    assert service._task_env("pdf_docx_routing") == {}
