from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from skill_toolbox.models import ProviderConfig
from skill_toolbox.providers import create_provider
from skill_toolbox.providers.base import ModelProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest, UserInputBroker
from skill_toolbox.unicode_utils import sanitize_data

Emitter = Callable[[dict[str, Any]], None]
ProviderFactory = Callable[[ProviderConfig], ModelProvider]
DEBUG_LOG_DIR = ".skill-toolbox-logs"


class SidecarService:
    def __init__(
        self, emit: Emitter, provider_factory: ProviderFactory = create_provider
    ) -> None:
        self.emit = emit
        self.provider_factory = provider_factory
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.brokers: dict[str, UserInputBroker] = {}

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
            skill_id=str(payload["skill_id"]),
            user_prompt=str(payload.get("user_prompt", "")),
            output_dir=output_dir,
            materials=materials,
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
