import asyncio
from pathlib import Path

import pytest
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.sidecar import SidecarService


@pytest.mark.asyncio
async def test_sidecar_runs_task_and_emits_correlated_events(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write",
                        arguments={
                            "path": "work/spec.json",
                            "content": """{
                              "title":"Sidecar 测试","summary":"测试",
                              "table":{"headers":["A"],"rows":[["B"]]},
                              "formula":"x = 1","code":"print(1)"
                            }""",
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="build",
                        name="exec_cmd",
                        arguments={
                            "action": "build_simple_docx",
                            "args": {
                                "spec_path": "work/spec.json",
                                "output_path": "artifacts/sidecar.docx",
                            },
                        },
                    )
                ]
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="finish",
                        name="finish_task",
                        arguments={"artifacts": ["artifacts/sidecar.docx"]},
                    )
                ]
            ),
        ]
    )
    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: provider)

    await service.handle(
        {
            "id": "task-1",
            "type": "start_task",
            "payload": {
                "provider": {"kind": "mock", "model": "mock"},
                "skill_id": "simple_docx",
                "user_prompt": "测试",
                "output_dir": str(tmp_path),
            },
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    assert (tmp_path / "sidecar.docx").exists()
    assert all(event["id"] == "task-1" for event in events)
    assert events[-1]["event"]["type"] == "task_completed"
