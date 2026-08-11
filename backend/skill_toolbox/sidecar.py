from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from skill_toolbox.capabilities import (
    missing_required,
    probe_fingerprint,
    resolve_capabilities,
)
from skill_toolbox.models import CapabilityProbeReport, ProbeResult, ProviderConfig
from skill_toolbox.providers import create_provider
from skill_toolbox.providers.base import ModelProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest, UserInputBroker
from skill_toolbox.skills import load_skill
from skill_toolbox.tools import resolve_mineru_cli
from skill_toolbox.unicode_utils import redact_secrets, sanitize_data

Emitter = Callable[[dict[str, Any]], None]
ProviderFactory = Callable[[ProviderConfig], ModelProvider]
DEBUG_LOG_DIR = ".skill-toolbox-logs"
MODELS_TIMEOUT_SECONDS = 15
# 能力探测整体超时：三次最小请求（tool/vision/reasoning）串行执行。
PROBE_TIMEOUT_SECONDS = 60.0

# 稳定错误码（实施计划 §9.1）。前端按码渲染可行动提示；原始 stderr 只进
# 调试日志，不进事件正文。
TASK_ERROR_CODES = {
    "MINERU_CLI_MISSING",
    "MINERU_TOKEN_MISSING",
    "MINERU_PARSE_FAILED",
    "MODEL_TOOL_CALLING_REQUIRED",
    "MATERIAL_UNSUPPORTED",
    "MATERIAL_CORRUPT",
}

# MinerU preflight 只做"入口可解析"这一离线判定；token 是否有效由
# extract 调用本身（带 token 时）暴露，不发起额外网络请求。
def _mineru_preflight() -> None:
    """校验 MinerU CLI 可执行入口可解析。失败抛 RuntimeError，带稳定错误码。"""
    try:
        resolve_mineru_cli()
    except RuntimeError as exc:
        raise RuntimeError(f"[MINERU_CLI_MISSING] {exc}") from None


def _capability_failure(skill_id: str, missing: list[str], model: str) -> str:
    code = "MODEL_TOOL_CALLING_REQUIRED" if missing else ""
    detail = (
        f"Skill '{skill_id}' requires capability '{missing[0]}' but the "
        f"configured model ({model or 'unknown'}) does not provide it. "
        "Switch to a capable model in Settings or choose a different skill."
    )
    return f"[{code}] {detail}"


def _EMPTY_REPORT(checked_at: float = 0.0) -> CapabilityProbeReport:
    """A fresh all-unknown report, stamped with the current time."""
    return CapabilityProbeReport(
        tool_calling=ProbeResult(checked_at=checked_at),
        vision=ProbeResult(checked_at=checked_at),
        reasoning_control=ProbeResult(checked_at=checked_at),
    )


def _redact(text: str | None) -> str:
    """Strip API keys / tokens before any probe detail reaches the UI or DB."""
    return redact_secrets(text or "")

# 产物文件名的 skill 来源标记 → 前端工具页。runtime._publish 会把标记打进
# 文件名（如 resume.resume_pro.docx），_list_artifacts 据此按产生它的功能
# 归类；无标记的历史文件进 unmarked，由前端按扩展名兜底。
_SKILL_MARKERS: tuple[tuple[str, str], ...] = (
    ("ppt", ".ppt-master."),
    ("resume", ".resume_pro."),
    ("docx", ".docx_pro."),
    ("pdf", ".pdf_docx_routing."),
)


def _EMPTY_BY_SKILL() -> dict[str, list[str]]:
    return {"ppt": [], "resume": [], "docx": [], "pdf": [], "unmarked": []}


def request_models(kind: str, base_url: str, api_key: str) -> tuple[list[str], str | None]:
    """GET the provider's model list and return (model_ids, error).

    OpenAI / OpenAI-compatible:  GET {base}/models, Bearer auth, data[].id.
    Anthropic:                   GET {base}/v1/models (or {base}/models when the
                                 configured base already ends with /v1), x-api-key
                                 auth + anthropic-version header.

    Runs in a worker thread — never block the asyncio loop with it.
    """
    base = (base_url or "").rstrip("/")
    if kind == "anthropic":
        base = base or "https://api.anthropic.com"
        endpoint = f"{base}/v1/models" if not base.endswith("/v1") else f"{base}/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
    else:
        if not base:
            return [], "base_url 为空"
        endpoint = f"{base}/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
    request = urllib.request.Request(endpoint, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=MODELS_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], str(exc)
    models = [item.get("id") for item in data.get("data", []) if item.get("id")]
    return models, None


class SidecarService:
    def __init__(
        self, emit: Emitter, provider_factory: ProviderFactory = create_provider
    ) -> None:
        self.emit = emit
        self.provider_factory = provider_factory
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.brokers: dict[str, UserInputBroker] = {}
        # 独立运行的 capability probe 任务（不阻塞 stdin 消息循环）。
        self.probes: dict[str, asyncio.Task[None]] = {}
        # MinerU token from Settings. Held in memory only and passed through the
        # TaskRequest side channel to MaterialService; Agent exec_cmd never sees it.
        self._mineru_key: str | None = None

    def _set_mineru_key(self, request_id: str, payload: dict[str, Any]) -> None:
        self._mineru_key = str(payload.get("mineru_key", "")).strip() or None
        self._emit(
            request_id,
            {"type": "mineru_key_updated", "configured": self._mineru_key is not None},
        )

    def _task_env(self, skill_id: str) -> dict[str, str]:
        """Agent-visible script env; MinerU credentials use TaskRequest instead."""
        del skill_id
        return {}

    def mineru_ready(self) -> bool:
        """MinerU CLI 入口是否可解析（供前端 preflight 状态显示）。"""
        try:
            _mineru_preflight()
            return True
        except RuntimeError:
            return False

    def mineru_status(self) -> dict[str, bool]:
        """前端 preflight 状态：CLI 可解析 + Token 已配置（§12.3）。
        Token 缺失也要在任务开始前提示，不能只查 CLI。"""
        return {
            "ok": self.mineru_ready(),
            "token_configured": bool(self._mineru_key),
        }

    async def handle(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("id", ""))
        message_type = message.get("type")
        payload = message.get("payload") or {}
        if not request_id:
            self.emit(
                {
                    "id": "",
                    "event": {"type": "protocol_error", "error": "Missing request id"},
                }
            )
            return
        try:
            if message_type == "start_task":
                await self._start_task(request_id, payload)
            elif message_type == "answer_questions":
                self._answer_questions(request_id, payload)
            elif message_type == "cancel_task":
                self._cancel_task(request_id)
            elif message_type == "ping":
                self._emit(request_id, {"type": "pong"})
            elif message_type == "set_mineru_key":
                self._set_mineru_key(request_id, payload)
            elif message_type == "clear_mineru_key":
                self._mineru_key = None
                self._emit(request_id, {"type": "mineru_key_cleared"})
            elif message_type == "mineru_status":
                self._emit(request_id, {"type": "mineru_status", **self.mineru_status()})
            elif message_type == "fetch_models":
                await self._fetch_models(request_id, payload)
            elif message_type == "list_artifacts":
                await self._list_artifacts(request_id, payload)
            elif message_type == "probe_capabilities":
                await self._probe_capabilities(request_id, payload)
            else:
                self._emit(
                    request_id,
                    {
                        "type": "protocol_error",
                        "error": f"Unknown message type: {message_type}",
                    },
                )
        except (KeyError, TypeError, ValueError) as exc:
            self._emit(request_id, {"type": "protocol_error", "error": str(exc)})

    async def _start_task(self, request_id: str, payload: dict[str, Any]) -> None:
        if request_id in self.tasks and not self.tasks[request_id].done():
            self._emit(
                request_id,
                {"type": "protocol_error", "error": "Task id is already active"},
            )
            return
        config = ProviderConfig.model_validate(payload["provider"])
        skill = load_skill(str(payload["skill_id"]))
        capabilities = resolve_capabilities(config)
        missing = missing_required(skill.required_capabilities, capabilities)
        if missing:
            self._emit(
                request_id,
                {"type": "task_failed", "error": _capability_failure(skill.id, missing, config.model)},
            )
            return
        # MinerU 全局 preflight（实施计划 §3.1/§9.1）：四个功能都是 MinerU
        # 强依赖，CLI 不可用或 Token 未配置直接在 Agent loop 前失败，不进入
        # 运行时（Token 缺失返回 MINERU_TOKEN_MISSING 稳定码）。
        if not self._mineru_key:
            self._emit(
                request_id,
                {
                    "type": "task_failed",
                    "error": "[MINERU_TOKEN_MISSING] 未配置 MinerU API Token："
                    "请在设置页「第三方服务」填写 Token 后重试。",
                },
            )
            return
        try:
            _mineru_preflight()
        except RuntimeError as exc:
            self._emit(request_id, {"type": "task_failed", "error": str(exc)})
            return
        provider = self.provider_factory(config)
        broker = UserInputBroker()
        self.brokers[request_id] = broker
        materials = [Path(p) for p in payload.get("materials", []) if p]
        output_dir = Path(payload["output_dir"])
        log_path = output_dir / DEBUG_LOG_DIR / f"{request_id}.jsonl"
        log_lock = threading.Lock()

        def debug_logger(entry: dict[str, Any]) -> None:
            """Append a full debug entry to disk and broadcast a truncated summary."""
            stamped = {**entry, "ts": time.time()}
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(stamped, ensure_ascii=False)
                with log_lock, log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # logging must never crash the task
            summary = self._summarise_debug(stamped)
            self._emit(request_id, {"type": "debug_log", "entry": summary})

        runtime = AgentRuntime(
            provider=provider,
            input_broker=broker,
            emit=lambda event: self._emit(request_id, event),
            debug_logger=debug_logger,
        )
        request = TaskRequest(
            skill_id=skill.id,
            user_prompt=str(payload.get("user_prompt", "")),
            output_dir=output_dir,
            materials=materials,
            capabilities=capabilities,
            env=self._task_env(skill.id),
            mineru_token=self._mineru_key,
        )
        self._emit(request_id, {"type": "debug_log_path", "path": str(log_path)})
        task = asyncio.create_task(self._run(request_id, runtime, request, log_path))
        self.tasks[request_id] = task

    async def _run(
        self,
        request_id: str,
        runtime: AgentRuntime,
        request: TaskRequest,
        log_path: Path,
    ) -> None:
        try:
            await runtime.run(request)
        except asyncio.CancelledError:
            self._emit(request_id, {"type": "task_cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - task failures must not terminate the Sidecar
            self._emit(request_id, {"type": "task_failed", "error": str(exc)})
        finally:
            self.brokers.pop(request_id, None)
            self.tasks.pop(request_id, None)

    def _answer_questions(self, request_id: str, payload: dict[str, Any]) -> None:
        broker = self.brokers.get(request_id)
        if broker is None:
            self._emit(
                request_id,
                {"type": "protocol_error", "error": "Task is not awaiting answers"},
            )
            return
        try:
            broker.answer(dict(payload.get("answers", {})))
        except RuntimeError as exc:
            self._emit(request_id, {"type": "protocol_error", "error": str(exc)})

    def _cancel_task(self, request_id: str) -> None:
        task = self.tasks.get(request_id)
        if task is None or task.done():
            self._emit(
                request_id, {"type": "protocol_error", "error": "Task is not active"}
            )
            return
        task.cancel()

    async def _list_artifacts(self, request_id: str, payload: dict[str, Any]) -> None:
        """List previously generated artifact files in the configured output dir.

        The UI shows "产物版本" from the last task's completion event only; on
        app start (or when switching to the artifacts tab) the frontend asks
        for anything already sitting in the output directory, so earlier
        results reappear. Discovery is limited to the immediate output dir —
        files are published flat (runtime._publish), so a flat list is the
        contract; nothing recursive, nothing outside the dir.

        Return shape: `files` (all, ordered by mtime desc) kept for
        backward-compat, plus `by_skill` bucketing — runtime._publish stamps
        each artifact name with its source skill (e.g. `resume.resume_pro.docx`),
        so the frontend can route each file to the tool page that produced it
        instead of guessing by extension. Unstamped legacy files land in
        `unmarked`.
        """
        raw = str(payload.get("output_dir", "")).strip()
        empty = {"type": "artifacts_listed", "files": [], "by_skill": _EMPTY_BY_SKILL()}
        if not raw:
            self._emit(request_id, empty)
            return
        directory = Path(raw)
        if not directory.is_dir():
            self._emit(request_id, empty)
            return
        files: list[str] = []
        try:
            for path in directory.iterdir():
                if path.is_file():
                    name = path.name.lower()
                    if name.endswith((".docx", ".doc", ".pdf", ".pptx", ".ppt", ".md", ".txt")):
                        files.append(str(path.resolve()))
        except OSError:
            pass
        files.sort(
            key=lambda p: (-self._stat_mtime(p), p.casefold())
        )
        by_skill: dict[str, list[str]] = _EMPTY_BY_SKILL()
        for absolute in files:
            placed = False
            for tool, marker in _SKILL_MARKERS:
                if marker in absolute:
                    by_skill[tool].append(absolute)
                    placed = True
                    break
            if not placed:
                by_skill["unmarked"].append(absolute)
        self._emit(request_id, {"type": "artifacts_listed", "files": files, "by_skill": by_skill})

    @staticmethod
    def _stat_mtime(path: str) -> float:
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return 0.0

    async def _fetch_models(self, request_id: str, payload: dict[str, Any]) -> None:
        """Proxy the provider's model-list endpoint and emit the model ids.

        Runs in a worker thread so the asyncio loop never blocks on a slow
        gateway. The api_key stays in the payload; it is never persisted.
        """
        kind = str(payload.get("kind", "openai"))
        base_url = str(payload.get("base_url", ""))
        api_key = str(payload.get("api_key", ""))
        models, error = await asyncio.to_thread(request_models, kind, base_url, api_key)
        if error:
            self._emit(request_id, {"type": "models_fetched", "models": [], "error": error})
            return
        self._emit(request_id, {"type": "models_fetched", "models": models})

    async def _probe_capabilities(self, request_id: str, payload: dict[str, Any]) -> None:
        """Run the three real capability probes for the current provider.

        Dispatched as an independent asyncio task so the stdin message loop is
        never blocked for the (up to PROBE_TIMEOUT_SECONDS) probe window. The
        api_key lives only in memory — every detail/error emitted is redacted,
        and the report is persisted by the frontend, never here.

        The response carries a fingerprint snapshot of the probe request
        (kind + base_url + model). The frontend persists the report only if the
        provider is still on that fingerprint — a probe started on model A whose
        answer arrives after the user switched to provider B must be dropped,
        not written into B's row.
        """
        config = ProviderConfig.model_validate(payload["provider"])
        if config.kind == "mock":
            self._emit(
                request_id,
                {"type": "protocol_error", "error": "mock provider cannot be probed"},
            )
            return
        fingerprint = probe_fingerprint(config)
        task = asyncio.create_task(self._run_probe(request_id, config, fingerprint))
        self.probes[request_id] = task
        task.add_done_callback(lambda _t: self.probes.pop(request_id, None))

    async def _run_probe(
        self,
        request_id: str,
        config: ProviderConfig,
        fingerprint: str,
    ) -> None:
        now = time.time()

        def empty_report() -> CapabilityProbeReport:
            return _EMPTY_REPORT(now)

        try:
            provider = self.provider_factory(config)
        except Exception as exc:  # noqa: BLE001
            detail = _redact(str(exc))[:200]
            self._emit(
                request_id,
                {
                    "type": "capabilities_probed",
                    "fingerprint": fingerprint,
                    "report": empty_report().model_dump(),
                    "error": f"provider init failed: {detail}",
                },
            )
            return
        try:
            raw = await asyncio.wait_for(provider.probe(), timeout=PROBE_TIMEOUT_SECONDS)
        except TimeoutError:
            report = empty_report()
            for capability in ("tool_calling", "vision", "reasoning_control"):
                setattr(
                    report,
                    capability,
                    ProbeResult(status="probe_error", checked_at=now, detail=f"timed out after {PROBE_TIMEOUT_SECONDS:g}s"),
                )
            self._emit(
                request_id,
                {
                    "type": "capabilities_probed",
                    "fingerprint": fingerprint,
                    "report": report.model_dump(),
                    "error": f"probe timed out after {PROBE_TIMEOUT_SECONDS:g}s",
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 - a failed probe must not kill the Sidecar
            detail = _redact(str(exc))[:200]
            report = empty_report()
            for capability in ("tool_calling", "vision", "reasoning_control"):
                setattr(report, capability, ProbeResult(status="probe_error", checked_at=now, detail=detail))
            self._emit(
                request_id,
                {
                    "type": "capabilities_probed",
                    "fingerprint": fingerprint,
                    "report": report.model_dump(),
                    "error": detail,
                },
            )
            return
        report = empty_report()
        fields = set(CapabilityProbeReport.model_fields)
        for capability, (status, detail) in raw.items():
            if capability not in fields:
                continue
            safe_detail = _redact(detail)[:200] if detail else None
            result = ProbeResult(status=status, checked_at=now, detail=safe_detail)
            # Reasoning Control 探测的 detail 槽位承载验证成功的 control 类型
            # （effort/budget/adaptive）；只接受白名单值，其它一律不写入 control，
            # 避免任意 detail 文本污染 Literal 字段导致校验失败丢消息。
            if (
                capability == "reasoning_control"
                and status == "verified"
                and safe_detail in {"effort", "budget", "adaptive"}
            ):
                result.control = safe_detail  # type: ignore[assignment]
            setattr(report, capability, result)
        # 只把探测结果发回前端；不在此持久化 —— 持久化由前端在
        # 指纹（kind/base_url/model）未变时经 save_provider 落库，避免双写竞态。
        self._emit(
            request_id,
            {"type": "capabilities_probed", "fingerprint": fingerprint, "report": report.model_dump()},
        )

    async def wait_all(self) -> None:
        active = [task for task in self.tasks.values() if not task.done()]
        active.extend(task for task in self.probes.values() if not task.done())
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def _emit(self, request_id: str, event: dict[str, Any]) -> None:
        self.emit({"id": request_id, "event": event})

    _PREVIEW_LIMIT = 200

    @classmethod
    def _summarise_debug(cls, entry: dict[str, Any]) -> dict[str, Any]:
        """Project a verbose debug entry into a small UI-safe summary."""
        phase = entry.get("phase", "unknown")
        ts = entry.get("ts", 0.0)
        if phase == "task_start":
            return {
                "ts": ts,
                "phase": phase,
                "skill_id": entry.get("skill_id"),
                "materials_count": len(entry.get("materials", [])),
                "user_prompt_preview": cls._clip(entry.get("user_prompt", "")),
            }
        if phase == "materials_staged":
            return {
                "ts": ts,
                "phase": phase,
                "staged": [
                    item.get("relative") for item in entry.get("staged", [])
                ],
            }
        if phase == "model_turn":
            return {
                "ts": ts,
                "phase": phase,
                "step": entry.get("step"),
                "assistant_text_preview": cls._clip(entry.get("assistant_text", "")),
                "tool_calls": [
                    {"name": tc.get("name"), "args_preview": cls._clip(tc.get("arguments", ""))}
                    for tc in entry.get("tool_calls", [])
                ],
            }
        if phase == "tool_call":
            return {
                "ts": ts,
                "phase": phase,
                "tool": entry.get("tool"),
                "args_preview": cls._clip(entry.get("arguments", "")),
            }
        if phase == "tool_result":
            return {
                "ts": ts,
                "phase": phase,
                "tool": entry.get("tool"),
                "success": entry.get("success"),
                "content_preview": cls._clip(entry.get("content", "")),
                "error": entry.get("error"),
            }
        return {"ts": ts, "phase": phase}

    @classmethod
    def _clip(cls, value: Any) -> str:
        if value is None:
            return ""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text if len(text) <= cls._PREVIEW_LIMIT else text[: cls._PREVIEW_LIMIT] + "…"


def _stdin_reader(
    loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None]
) -> None:
    for line in sys.stdin:
        loop.call_soon_threadsafe(queue.put_nowait, line)
    loop.call_soon_threadsafe(queue.put_nowait, None)


async def _serve() -> None:
    lock = threading.Lock()

    def emit(message: dict[str, Any]) -> None:
        with lock:
            print(json.dumps(sanitize_data(message), ensure_ascii=False), flush=True)

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    thread = threading.Thread(target=_stdin_reader, args=(loop, queue), daemon=True)
    thread.start()
    service = SidecarService(emit)
    emit({"id": "system", "event": {"type": "ready"}})
    while True:
        line = await queue.get()
        if line is None:
            break
        try:
            message = json.loads(line)
            await service.handle(message)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            emit({"id": "", "event": {"type": "protocol_error", "error": str(exc)}})
    await service.wait_all()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
