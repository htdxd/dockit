from pathlib import Path

import pytest
from skill_toolbox.models import ToolCall
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.tools import ToolRegistry


@pytest.mark.asyncio
async def test_tool_call_replaces_unpaired_surrogate_before_write(
    tmp_path: Path,
) -> None:
    call = ToolCall(
        id="write-invalid-unicode",
        name="write",
        arguments={"path": "work/content.txt", "content": "before\udcafafter"},
    )
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())

    result = await registry.execute(call)

    assert result.success is True
    assert (tmp_path / "work" / "content.txt").read_text(encoding="utf-8") == (
        "before\ufffdafter"
    )
