from test_resume_workflow_service import workflow
import asyncio
import dataclasses
from pathlib import Path

import pytest
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.sidecar import SidecarService
from skill_toolbox.skills import load_skill

EMPTY_DOCX_PLAN = (
    '{"schema_version":"1","task_type":"resume","mode":"conservative",'
    '"selections":[],"exclusions":[],"questions_asked":false}'
)


@pytest.mark.asyncio
@pytest.mark.parametrize('directory', ['', None, './results'])
async def test_default_output_is_resolved_against_writable_backend_directory(tmp_path, monkeypatch, directory):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('skill_toolbox.sidecar._mineru_preflight', lambda: None)
    service = SidecarService(lambda _: None, provider_factory=lambda _: ScriptedProvider([]))
    service._mineru_key = 'test-token'
    captured = []
    async def capture(request_id, runtime, request, log_path):
        captured.append(request.output_dir)
    monkeypatch.setattr(service, '_run', capture)
    await service.handle({'id':'default-directory','type':'start_task','payload':{
        'provider':{'kind':'mock','model':'mock'}, 'skill_id':'resume_pro',
        'template_id':'t001', 'user_prompt':'test', 'output_dir':directory}})
    await service.wait_all()
    assert captured == [(tmp_path / ('results' if directory else 'outputs')).resolve()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template_id, expected",
    [("t109", "t109"), (" t001 ", "t001"), ("", None), (None, None), (109, None)],
)
async def test_sidecar_passes_structured_resume_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template_id: object, expected: str | None
) -> None:
    captured = []

    async def capture_run(request_id, runtime, request, log_path):
        captured.append(request)

    monkeypatch.setattr("skill_toolbox.sidecar._mineru_preflight", lambda: None)
    service = SidecarService(lambda event: None, provider_factory=lambda _: ScriptedProvider([]))
    service._mineru_key = "test-token"
    monkeypatch.setattr(service, "_run", capture_run)
    await service.handle({
        "id": "template-selection",
        "type": "start_task",
        "payload": {
            "provider": {"kind": "mock", "model": "mock"},
            "skill_id": "resume_pro",
            "template_id": template_id,
            "writing_style": "strong",
            "user_prompt": "简历模板：t109",
            "output_dir": str(tmp_path),
        },
    })
    await service.wait_all()
    assert len(captured) == 1
    assert captured[0].template_id == expected
    assert captured[0].writing_style == "strong"
    assert captured[0].user_prompt == "简历模板：t109"


@pytest.mark.asyncio
async def test_sidecar_runs_task_and_emits_correlated_events(tmp_path, workflow):
    class Provider:
        step = 0
        candidate = None
        path = None
        async def complete(self, system, messages, tools):
            self.step += 1
            if self.step == 1:
                name, args = 'resume_generate', {'content':{'sections':[{
                    'key':'skills','title':'技能','entries':[{'text':['Python']}]}]}}
            elif self.step == 2:
                import json
                data = json.loads(messages[-1].tool_results[0].content)['data']
                self.candidate, self.path = data['candidate_id'], data['docx']
                name,args = 'resume_accept', {'candidate_id':self.candidate,'visual_notes':'已检查全部页面'}
            else:
                name,args = 'finish_task', {'artifacts':[self.path]}
            return AssistantTurn(tool_calls=[ToolCall(id=str(self.step),name=name,arguments=args)])
    events=[]
    service=SidecarService(events.append,provider_factory=lambda _:Provider())
    service._set_mineru_key('task-1',{'mineru_key':'test-token'})
    await service.handle({'id':'task-1','type':'start_task','payload':{
        'provider':{'kind':'mock','model':'mock'},'skill_id':'resume_pro',
        'template_id':workflow.template_id,'user_prompt':'测试','output_dir':str(tmp_path)}})
    await asyncio.wait_for(service.wait_all(),timeout=10)
    assert (tmp_path/'resume.resume_pro.docx').exists()
    assert all(event['id']=='task-1' for event in events)
    assert events[-1]['event']['type']=='task_completed'



@pytest.mark.asyncio
async def test_sidecar_accepts_skill_with_satisfied_capabilities(tmp_path: Path) -> None:
    """docx_pro requires tool_calling (always assumed present);
    a model that satisfies them passes the gate and the task starts."""
    events: list[dict[str, object]] = []
    provider = ScriptedProvider([])  # empty → runtime will fail on no tool calls; gate must not reject
    service = SidecarService(events.append, provider_factory=lambda _: provider)
    # MinerU 强依赖（§3.1）：配置 Token 以通过 preflight
    service._set_mineru_key("task-gated", {"mineru_key": "test-token"})

    await service.handle(
        {
            "id": "task-gated",
            "type": "start_task",
            "payload": {
                "provider": {"kind": "openai", "model": "gpt-4o"},
                "skill_id": "resume_pro",
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


@pytest.mark.asyncio
async def test_sidecar_requires_mineru_token(tmp_path: Path) -> None:
    """未配置 MinerU Token 时任务在 Agent loop 前失败（§3.1 fail-fast）。"""
    events: list[dict[str, object]] = []
    provider = ScriptedProvider([])
    service = SidecarService(events.append, provider_factory=lambda _: provider)

    await service.handle(
        {
            "id": "task-notoken",
            "type": "start_task",
            "payload": {
                "provider": {"kind": "openai", "model": "gpt-4o"},
                "skill_id": "resume_pro",
                "user_prompt": "生成报告",
                "output_dir": str(tmp_path),
            },
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    failed = [e["event"] for e in events if e["event"].get("type") == "task_failed"]
    assert failed and "MINERU_TOKEN_MISSING" in str(failed[0].get("error", ""))
    started = [e["event"] for e in events if e["event"].get("type") == "task_started"]
    assert started == []


def test_request_models_openai_endpoint_and_auth(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """OpenAI route: GET {base}/models with Bearer auth, parse data[].id."""

    from skill_toolbox import sidecar

    captured: dict[str, object] = {}

    import httpx
    original = sidecar.openai.OpenAI
    def respond(request):
        captured["url"] = str(request.url)
        captured["header"] = request.headers.get("Authorization")
        return httpx.Response(200, json={'data': [{'id':'gpt-4o'}, {'id':'deepseek-v3'}, {'id':None}]})
    monkeypatch.setattr(sidecar.openai, 'OpenAI', lambda **kw: original(**kw,
        http_client=httpx.Client(transport=httpx.MockTransport(respond))))

    models, error = sidecar.request_models("openai", "https://gw.example/v1", "sk-test")
    assert error is None
    assert models == ["gpt-4o", "deepseek-v3"]
    assert captured["url"] == "https://gw.example/v1/models"
    assert captured["header"] == "Bearer sk-test"


def test_request_models_anthropic_endpoint_and_auth(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Anthropic route: /v1/models with x-api-key + anthropic-version."""
    from skill_toolbox import sidecar

    captured: dict[str, object] = {}

    import httpx
    original = sidecar.anthropic.Anthropic
    def respond(request):
        captured["url"] = str(request.url)
        captured["x-api-key"] = request.headers.get("x-api-key")
        captured["version"] = request.headers.get("anthropic-version")
        return httpx.Response(200, json={'data':[{'id':'claude-4-sonnet'}, {'id':'claude-4-haiku'}]})
    monkeypatch.setattr(sidecar.anthropic, 'Anthropic', lambda **kw: original(**kw,
        http_client=httpx.Client(transport=httpx.MockTransport(respond))))

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
    from skill_toolbox import sidecar

    import httpx
    original = sidecar.openai.OpenAI
    monkeypatch.setattr(sidecar.openai, 'OpenAI', lambda **kw: original(**kw,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401)))))

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


# ===== probe_capabilities 消息协议 =====

class _ProbingProvider:
    """Fake provider: returns canned probe results; records the request kinds."""

    def __init__(self, results: dict[str, tuple[str, str | None]]) -> None:
        self.results = results
        self.requested: list[str] = []

    async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
        del system_prompt, messages, tools
        return AssistantTurn(text="n/a")

    async def probe(
        self,
        capabilities: list[str] | None = None,
    ) -> dict[str, tuple[str, str | None]]:
        self.requested = list(capabilities or [])
        return self.results


def _probe_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": {
            "kind": "openai",
            "model": "gpt-4o",
            "api_key": "sk-test",
            "base_url": "https://api.example.com/v1",
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_probe_capabilities_emits_versioned_report() -> None:
    provider = _ProbingProvider(
        {
            "tool_calling": ("verified", None),
            "vision": ("unsupported", "image input rejected"),
            "reasoning_control": ("verified", "effort"),
        }
    )
    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: provider)

    await service.handle(
        {
            "id": "probe-1",
            "type": "probe_capabilities",
            "payload": _probe_payload(),
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    probed = [e["event"] for e in events if e["event"].get("type") == "capabilities_probed"]
    assert len(probed) == 1
    report = probed[0]["report"]
    assert report["tool_calling"]["status"] == "verified"
    assert report["vision"]["status"] == "unsupported"
    assert report["vision"]["detail"] == "image input rejected"
    assert report["reasoning_control"]["status"] == "verified"
    assert report["reasoning_control"]["control"] == "effort"
    assert report["tool_calling"]["probe_version"] == 1
    assert report["tool_calling"]["checked_at"] > 0
    # 探测报告绝不携带 API Key / 原始 CoT
    assert "sk-test" not in str(probed[0])
    assert "error" not in probed[0]


@pytest.mark.asyncio
async def test_probe_provider_error_emits_report_without_crashing() -> None:
    class _BoomProvider:
        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            del system_prompt, messages, tools
            return AssistantTurn()

        async def probe(self, capabilities=None):  # type: ignore[no-untyped-def]
            del capabilities
            raise RuntimeError("connection refused")

    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: _BoomProvider())

    await service.handle(
        {
            "id": "probe-2",
            "type": "probe_capabilities",
            "payload": _probe_payload(),
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    probed = [e["event"] for e in events if e["event"].get("type") == "capabilities_probed"]
    assert len(probed) == 1
    report = probed[0]["report"]
    assert report["tool_calling"]["status"] == "probe_error"
    assert report["vision"]["status"] == "probe_error"
    assert report["reasoning_control"]["status"] == "probe_error"
    # 失败不终止 Sidecar：handle 正常返回，服务仍可用
    assert events[-1]["id"] == "probe-2"


@pytest.mark.asyncio
async def test_probe_timeout_marks_all_probe_error() -> None:
    class _SlowProvider:
        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            del system_prompt, messages, tools
            return AssistantTurn()

        async def probe(self, capabilities=None):  # type: ignore[no-untyped-def]
            del capabilities
            await asyncio.sleep(5)
            return {}

    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: _SlowProvider())
    # 把超时压到 50ms，模拟真实慢网关
    import skill_toolbox.sidecar as sidecar_mod
    old = sidecar_mod.PROBE_TIMEOUT_SECONDS
    sidecar_mod.PROBE_TIMEOUT_SECONDS = 0.05
    try:
        await service.handle(
            {
                "id": "probe-3",
                "type": "probe_capabilities",
                "payload": _probe_payload(),
            }
        )
        await asyncio.wait_for(service.wait_all(), timeout=5)
    finally:
        sidecar_mod.PROBE_TIMEOUT_SECONDS = old

    probed = [e["event"] for e in events if e["event"].get("type") == "capabilities_probed"]
    assert len(probed) == 1
    report = probed[0]["report"]
    assert report["tool_calling"]["status"] == "probe_error"
    assert "timed out" in (probed[0].get("error") or "")


@pytest.mark.asyncio
async def test_probe_mock_provider_rejected() -> None:
    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: _ProbingProvider({}))
    await service.handle(
        {
            "id": "probe-4",
            "type": "probe_capabilities",
            "payload": {
                "provider": {
                    "kind": "mock",
                    "model": "mock",
                    "api_key": "",
                }
            },
        }
    )

    errors = [e["event"] for e in events if e["event"].get("type") == "protocol_error"]
    assert len(errors) == 1
    assert "mock provider cannot be probed" in (errors[0].get("error") or "")


@pytest.mark.asyncio
async def test_probe_response_carries_fingerprint() -> None:
    """回包携带请求快照指纹（kind+base_url+model），前端据此校验再持久化。"""
    provider = _ProbingProvider({"tool_calling": ("verified", None)})
    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: provider)

    await service.handle(
        {
            "id": "probe-fp",
            "type": "probe_capabilities",
            "payload": _probe_payload(
                provider={
                    "kind": "openai",
                    "model": "gpt-4o",
                    "api_key": "sk-test",
                    "base_url": "https://api.example.com/v1/",
                }
            ),
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    probed = [e["event"] for e in events if e["event"].get("type") == "capabilities_probed"]
    assert len(probed) == 1
    fp = probed[0].get("fingerprint")
    assert fp == '["openai","https://api.example.com/v1","gpt-4o"]'


@pytest.mark.asyncio
async def test_probe_error_detail_is_redacted() -> None:
    """探测失败 detail 必须脱敏，绝不把 api_key 写进事件/DB。"""
    class _LeakyProvider:
        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            del system_prompt, messages, tools
            return AssistantTurn()

        async def probe(self, capabilities=None):  # type: ignore[no-untyped-def]
            del capabilities
            raise RuntimeError(
                "Error code: 401 - Incorrect API key provided: sk-leaktest-abcdef123456"
            )

    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: _LeakyProvider())

    await service.handle(
        {
            "id": "probe-leak",
            "type": "probe_capabilities",
            "payload": _probe_payload(api_key="sk-leaktest-abcdef123456"),
        }
    )
    await asyncio.wait_for(service.wait_all(), timeout=5)

    serialised = str(events)
    assert "sk-leaktest-abcdef123456" not in serialised
    assert "REDACTED" in serialised
    probed = [e["event"] for e in events if e["event"].get("type") == "capabilities_probed"]
    detail = probed[0]["report"]["tool_calling"]["detail"]
    assert "sk-leaktest" not in detail


@pytest.mark.asyncio
async def test_probe_runs_in_independent_task_not_blocking_stdin() -> None:
    """探测必须异步调度：探测挂起期间，stdin 循环仍能处理其它消息。"""
    started = asyncio.Event()

    class _BlockingProvider:
        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            del system_prompt, messages, tools
            return AssistantTurn()

        async def probe(self, capabilities=None):  # type: ignore[no-untyped-def]
            del capabilities
            started.set()
            await asyncio.sleep(5)  # 模拟慢网关

    events: list[dict[str, object]] = []
    service = SidecarService(events.append, provider_factory=lambda _: _BlockingProvider())

    probe_task = asyncio.create_task(
        service.handle(
            {
                "id": "probe-slow",
                "type": "probe_capabilities",
                "payload": _probe_payload(),
            }
        )
    )
    # 探测已经进入等待（独立任务），此时另一条消息必须能立即被处理
    await asyncio.wait_for(started.wait(), timeout=1)
    await service.handle(
        {
            "id": "ping-1",
            "type": "ping",
            "payload": {},
        }
    )
    await probe_task

    pongs = [e["event"] for e in events if e["event"].get("type") == "pong"]
    assert len(pongs) == 1  # ping 未被阻塞，说明探测不占 stdin 循环


@pytest.fixture(autouse=True)
def no_external_word_probe(monkeypatch):
    from skill_toolbox.tools.resume_edit import ResumeEditService
    from skill_toolbox.contracts.common import OperationResult
    monkeypatch.setattr(ResumeEditService,'prepare',lambda *a,**k:OperationResult(ok=True,data={'header_fields':['name','email']}))
