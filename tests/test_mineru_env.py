from pathlib import Path

import pytest
from skill_toolbox.models import ToolCall
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.sidecar import SidecarService

ECHO_ENV_SCRIPT = """\
import os, sys
print(os.environ.get("MINERU_TOKEN", "MISSING"))
sys.exit(0)
"""


def test_task_env_never_exposes_mineru_token_to_agent_scripts() -> None:
    """MinerU credentials travel only through TaskRequest's private field."""
    service = SidecarService(lambda _event: None)
    service._mineru_key = "sk-mineru"  # type: ignore[attr-defined]
    assert service._task_env("docx_pro") == {}
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
    assert service._task_env("docx_pro") == {}
    await service.handle({"id": "m2", "type": "clear_mineru_key", "payload": {}})
    assert service._mineru_key is None  # type: ignore[attr-defined]
    assert service._task_env("docx_pro") == {}
