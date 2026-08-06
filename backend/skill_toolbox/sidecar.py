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

from skill_toolbox.capabilities import missing_required, resolve_capabilities
from skill_toolbox.models import ProviderConfig
from skill_toolbox.providers import create_provider
from skill_toolbox.providers.base import ModelProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest, UserInputBroker
from skill_toolbox.skills import load_skill
from skill_toolbox.unicode_utils import sanitize_data

Emitter = Callable[[dict[str, Any]], None]
ProviderFactory = Callable[[ProviderConfig], ModelProvider]
DEBUG_LOG_DIR = ".skill-toolbox-logs"
MODELS_TIMEOUT_SECONDS = 15


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
        # MinerU token from the Settings page "第三方服务" tab. Held in memory
        # only (never persisted to disk by the backend), injected as the
        # MINERU_TOKEN env var into exec_cmd subprocesses for the PDF skill.
        self._mineru_key: str | None = None

    def _set_mineru_key(self, request_id: str, payload: dict[str, Any]) -> None:
        self._mineru_key = str(payload.get("mineru_key", "")).strip() or None
        self._emit(
            request_id,
            {"type": "mineru_key_updated", "configured": self._mineru_key is not None},
        )

    def _task_env(self, skill_id: str) -> dict[str, str]:
        """Env vars for a task's exec_cmd subprocesses. Currently: the MinerU
        token (Settings → 第三方服务) for the PDF skill only, so the key never
        reaches other skills' subprocesses."""
        if skill_id == "pdf_docx_routing" and self._mineru_key:
            return {"MINERU_TOKEN": self._mineru_key}
        return {}

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
            elif message_type == "fetch_models":
                await self._fetch_models(request_id, payload)
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
                {
                    "type": "task_failed",
                    "error": (
                        f"Skill '{skill.id}' requires capability "
                        f"'{missing[0]}' but the configured model "
                        f"({config.model or 'unknown'}) does not provide it. "
                        "Switch to a capable model in Settings or choose a "
                        "different skill."
                    ),
                },
            )
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

    async def wait_all(self) -> None:
        active = [task for task in self.tasks.values() if not task.done()]
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
