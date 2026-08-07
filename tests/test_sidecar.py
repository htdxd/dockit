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

    assert (tmp_path / "sidecar.simple_docx.docx").exists()
    assert all(event["id"] == "task-1" for event in events)
    assert events[-1]["event"]["type"] == "task_completed"


@pytest.mark.asyncio
async def test_sidecar_accepts_skill_with_satisfied_capabilities(tmp_path: Path) -> None:
    """docx_pro requires tool_calling+json_schema (always assumed present);
    a model that satisfies them passes the gate and the task starts."""
    events: list[dict[str, object]] = []
    provider = ScriptedProvider([])  # empty → runtime will fail on no tool calls; gate must not reject
    service = SidecarService(events.append, provider_factory=lambda _: provider)

    await service.handle(
        {
            "id": "task-gated",
            "type": "start_task",
            "payload": {
                "provider": {"kind": "openai", "model": "gpt-4o"},
                "skill_id": "docx_pro",
                "user_prompt": "生成报告",
                "output_dir": str(tmp_path),
            },
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    started = [e["event"] for e in events if e["event"].get("type") == "task_started"]
    assert len(started) == 1
    # Failure (if any) comes from the runtime loop, not a capability rejection.
    errors = [e["event"].get("error", "") for e in events if e["event"].get("type") == "task_failed"]
    assert all("capability" not in str(error) for error in errors)


def test_request_models_openai_endpoint_and_auth(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """OpenAI route: GET {base}/models with Bearer auth, parse data[].id."""
    from unittest.mock import MagicMock

    import skill_toolbox.sidecar as sidecar

    captured: dict[str, object] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"data":[{"id":"gpt-4o"},{"id":"deepseek-v3"},{"id":null}]}'

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args) -> None:  # noqa: ANN002
            return None

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        captured["url"] = request.full_url
        captured["header"] = request.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(sidecar.urllib.request, "urlopen", fake_urlopen)

    models, error = sidecar.request_models("openai", "https://gw.example/v1", "sk-test")
    assert error is None
    assert models == ["gpt-4o", "deepseek-v3"]
    assert captured["url"] == "https://gw.example/v1/models"
    assert captured["header"] == "Bearer sk-test"


def test_request_models_anthropic_endpoint_and_auth(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Anthropic route: /v1/models with x-api-key + anthropic-version."""
    import skill_toolbox.sidecar as sidecar

    captured: dict[str, object] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"data":[{"id":"claude-4-sonnet"},{"id":"claude-4-haiku"}]}'

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args) -> None:  # noqa: ANN002
            return None

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        headers = {k.lower(): v for k, v in request.headers.items()}
        captured["url"] = request.full_url
        captured["x-api-key"] = headers.get("x-api-key")
        captured["version"] = headers.get("anthropic-version")
        return FakeResponse()

    monkeypatch.setattr(sidecar.urllib.request, "urlopen", fake_urlopen)

    # base_url without /v1 → append /v1/models; empty base → official endpoint
    models, error = sidecar.request_models("anthropic", "https://api.anthropic.com", "sk-ant")
    assert error is None
    assert models == ["claude-4-sonnet", "claude-4-haiku"]
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["x-api-key"] == "sk-ant"
    assert captured["version"] == "2023-06-01"

    # base_url already ends with /v1 → don't double it
    models, error = sidecar.request_models("anthropic", "https://api.anthropic.com/v1", "sk-ant")
    assert error is None
    assert captured["url"] == "https://api.anthropic.com/v1/models"


def test_request_models_http_error_returns_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import urllib.error

    import skill_toolbox.sidecar as sidecar

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(sidecar.urllib.request, "urlopen", fake_urlopen)

    models, error = sidecar.request_models("openai", "https://gw.example/v1", "bad")
    assert models == []
    assert "401" in (error or "")


@pytest.mark.asyncio
async def test_list_artifacts_buckets_by_skill_marker(tmp_path: Path) -> None:
    """Stamped artifacts route to their producing skill page; legacy files to
    unmarked; each file appears exactly once across buckets."""
    names = [
        "resume.resume_pro.docx",
        "个人简历.pdf_docx_routing.docx",
        "deck.ppt-master.pptx",
        "报告.docx_pro.docx",
        "legacy.docx",  # 升级前的历史产物：无标记
        "notes.md",     # 不在扩展名白名单 → 不进 files
    ]
    for n in names:
        (tmp_path / n).write_text("x", encoding="utf-8")

    events: list[dict[str, object]] = []
    service = SidecarService(events.append)
    await service.handle(
        {
            "id": "scan-1",
            "type": "list_artifacts",
            "payload": {"output_dir": str(tmp_path)},
        }
    )

    listed = [e["event"] for e in events if e["event"].get("type") == "artifacts_listed"]
    assert len(listed) == 1
    payload = listed[0]
    assert len(payload["files"]) == 6  # 白名单含 .md，notes.md 也在内
    by_skill = payload["by_skill"]
    assert any(p.endswith("resume.resume_pro.docx") for p in by_skill["resume"])
    assert any(p.endswith("个人简历.pdf_docx_routing.docx") for p in by_skill["pdf"])
    assert any(p.endswith("deck.ppt-master.pptx") for p in by_skill["ppt"])
    assert any(p.endswith("报告.docx_pro.docx") for p in by_skill["docx"])
    assert sorted(Path(p).name for p in by_skill["unmarked"]) == ["legacy.docx", "notes.md"]
    # 每个文件只归一个桶
    seen: list[str] = []
    for bucket in by_skill.values():
        seen.extend(bucket)
    assert len(seen) == len(set(seen)) == 6


@pytest.mark.asyncio
async def test_list_artifacts_empty_dir_returns_empty_buckets(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    service = SidecarService(events.append)
    await service.handle(
        {
            "id": "scan-empty",
            "type": "list_artifacts",
            "payload": {"output_dir": str(tmp_path)},
        }
    )

    listed = [e["event"] for e in events if e["event"].get("type") == "artifacts_listed"]
    assert listed[0]["files"] == []
    assert listed[0]["by_skill"] == {
        "ppt": [], "resume": [], "docx": [], "pdf": [], "unmarked": []
    }
