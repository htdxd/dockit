from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skill_toolbox.models import ConversationMessage, ToolCall, ToolResult
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.providers.base import ModelProvider
from skill_toolbox.skills import load_skill
from skill_toolbox.tool_specs import TOOL_SPECS
from skill_toolbox.tools import ToolRegistry

EventEmitter = Callable[[dict[str, Any]], None]
DebugLogger = Callable[[dict[str, Any]], None]
MAX_PROGRESS_TEXT_CHARS = 2_000
MAX_LOG_ARG_CHARS = 2_000
MAX_LOG_RESULT_CHARS = 4_000
MAX_LOG_TEXT_CHARS = 2_000


def _truncate(value: Any, limit: int) -> str:
    """Render any JSON-serialisable value to a truncated string for debug logs."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"…<+{len(text) - limit} bytes>"


@dataclass(frozen=True)
class TaskRequest:
    skill_id: str
    user_prompt: str
    output_dir: Path
    materials: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class TaskResult:
    status: str
    artifacts: list[Path] = field(default_factory=list)
    error: str | None = None


class UserInputBroker:
    def __init__(self) -> None:
        self._future: asyncio.Future[dict[str, Any]] | None = None

    async def wait(self) -> dict[str, Any]:
        if self._future is not None and not self._future.done():
            raise RuntimeError("A question request is already pending")
        self._future = asyncio.get_running_loop().create_future()
        return await self._future

    def answer(self, answers: dict[str, Any]) -> None:
        if self._future is None or self._future.done():
            raise RuntimeError("No question request is pending")
        self._future.set_result(answers)


class AgentRuntime:
    def __init__(
        self,
        provider: ModelProvider,
        emit: EventEmitter,
        input_broker: UserInputBroker | None = None,
        model_timeout_seconds: float = 300.0,
        tool_timeout_seconds: float = 90.0,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        self.provider = provider
        self.emit = emit
        self.input_broker = input_broker
        self.model_timeout_seconds = model_timeout_seconds
        self.tool_timeout_seconds = tool_timeout_seconds
        self.debug = debug_logger or (lambda _entry: None)

    async def run(self, request: TaskRequest) -> TaskResult:
        skill = load_skill(request.skill_id)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        self.emit({"type": "task_started", "skill_id": skill.id})
        self.debug({
            "phase": "task_start",
            "skill_id": skill.id,
            "user_prompt": request.user_prompt,
            "output_dir": str(request.output_dir),
            "materials": [str(p) for p in request.materials],
        })
        with tempfile.TemporaryDirectory(prefix="skill-toolbox-") as temp:
            workspace = Path(temp)
            staged = self._stage_materials(workspace, request.materials)
            if staged:
                self.debug({
                    "phase": "materials_staged",
                    "workspace": str(workspace),
                    "sources_dir": str(workspace / "sources"),
                    "staged": [
                        {"relative": rel, "absolute": str(abs_)}
                        for rel, abs_ in staged
                    ],
                    "note": "Materials are available to the model under the 'sources/' "
                            "relative path; this is the only correct way to read them.",
                })
            policy = WorkspacePolicy(workspace, read_roots=(skill.dir,))
            tools = ToolRegistry(
                policy,
                skill.allowed_actions,
                skill_dir=skill.dir,
                scripts=skill.scripts,
            )
            messages: list[ConversationMessage] = []
            user_text = request.user_prompt or "按默认内容生成测试文档"
            if staged:
                files_list = "\n".join(f"  - {rel}" for rel, _ in staged)
                user_text = (
                    f"{user_text}\n\n"
                    f"你上传的材料已暂存到工作区，相对路径如下（用 read 或 exec_cmd 的 "
                    f"source 参数按此相对路径访问，不要猜其他路径）：\n{files_list}"
                )
            messages.append(ConversationMessage(role="user", text=user_text))
            text_only_turns = 0
            for step in range(1, skill.max_steps + 1):
                self.emit({"type": "model_started", "step": step})
                try:
                    turn = await asyncio.wait_for(
                        self.provider.complete(
                            skill.system_prompt, messages, TOOL_SPECS
                        ),
                        timeout=self.model_timeout_seconds,
                    )
                except TimeoutError:
                    return self._failed(
                        f"LLM request timed out after {self.model_timeout_seconds:g} seconds"
                    )
                self.emit(
                    {
                        "type": "model_finished",
                        "step": step,
                        "tool_call_count": len(turn.tool_calls),
                    }
                )
                self.debug({
                    "phase": "model_turn",
                    "step": step,
                    "assistant_text": _truncate(turn.text, MAX_LOG_TEXT_CHARS),
                    "tool_calls": [
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": _truncate(call.arguments, MAX_LOG_ARG_CHARS),
                        }
                        for call in turn.tool_calls
                    ],
                })
                messages.append(
                    ConversationMessage(
                        role="assistant", text=turn.text, tool_calls=turn.tool_calls
                    )
                )
                if turn.text:
                    progress_text = turn.text[:MAX_PROGRESS_TEXT_CHARS]
                    if len(turn.text) > MAX_PROGRESS_TEXT_CHARS:
                        progress_text += "\n[内容过长，已截断]"
                    self.emit({"type": "assistant_text", "text": progress_text})
                if not turn.tool_calls:
                    text_only_turns += 1
                    if text_only_turns >= 2:
                        return self._failed("Model stopped without calling finish_task")
                    messages.append(
                        ConversationMessage(
                            role="user",
                            text="任务尚未完成。请继续使用工具，并在产物验证后调用 finish_task。",
                        )
                    )
                    continue
                text_only_turns = 0
                finish = next(
                    (call for call in turn.tool_calls if call.name == "finish_task"),
                    None,
                )
                if finish is not None and len(turn.tool_calls) == 1:
                    self.emit({"type": "tool_started", "tool": finish.name})
                    try:
                        published = self._publish(finish, policy, request.output_dir)
                    except (OSError, ValueError) as exc:
                        result = self._tool_result(finish, False, str(exc))
                        self.emit(
                            {
                                "type": "tool_finished",
                                "tool": finish.name,
                                "success": False,
                            }
                        )
                        messages.append(
                            ConversationMessage(role="tool", tool_results=[result])
                        )
                        continue
                    self.emit(
                        {
                            "type": "tool_finished",
                            "tool": finish.name,
                            "success": True,
                        }
                    )
                    self.emit(
                        {
                            "type": "task_completed",
                            "artifacts": [str(path) for path in published],
                        }
                    )
                    return TaskResult(status="completed", artifacts=published)
                results = await self._execute_calls(turn.tool_calls, tools)
                messages.append(ConversationMessage(role="tool", tool_results=results))
            return self._failed(f"Task exceeded the {skill.max_steps}-step limit")

    async def _execute_calls(
        self, calls: list[ToolCall], tools: ToolRegistry
    ) -> list[ToolResult]:
        if all(call.name == "read" for call in calls):
            return await asyncio.gather(
                *(self._execute_call(call, tools) for call in calls)
            )
        results: list[ToolResult] = []
        for call in calls:
            results.append(await self._execute_call(call, tools))
        return results

    async def _execute_call(self, call: ToolCall, tools: ToolRegistry) -> ToolResult:
        self.emit({"type": "tool_started", "tool": call.name})
        self.debug({
            "phase": "tool_call",
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": _truncate(call.arguments, MAX_LOG_ARG_CHARS),
        })
        if call.name == "finish_task":
            result = self._tool_result(
                call, False, "finish_task must be the only tool call in its model turn"
            )
        elif call.name == "ask_user_questions":
            if self.input_broker is None:
                result = self._tool_result(call, False, "User input is unavailable")
            else:
                questions = call.arguments.get("questions", [])
                self.emit({"type": "questions_requested", "questions": questions})
                answers = await self.input_broker.wait()
                result = self._tool_result(
                    call, True, json.dumps(answers, ensure_ascii=False)
                )
        else:
            try:
                result = await asyncio.wait_for(
                    tools.execute(call), timeout=self.tool_timeout_seconds
                )
            except TimeoutError:
                result = self._tool_result(
                    call,
                    False,
                    f"Tool timed out after {self.tool_timeout_seconds:g} seconds",
                )
                self.emit(
                    {
                        "type": "tool_timeout",
                        "tool": call.name,
                        "timeout_seconds": self.tool_timeout_seconds,
                    }
                )
                self.debug({
                    "phase": "tool_result",
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "success": False,
                    "error": f"timed out after {self.tool_timeout_seconds:g}s",
                })
                return result
            except Exception as exc:  # noqa: BLE001 - keep one bad tool from killing the task
                result = self._tool_result(call, False, f"Tool failed: {exc}")
                self.emit({"type": "tool_failed", "tool": call.name, "error": str(exc)})
                self.debug({
                    "phase": "tool_result",
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return result
        self.emit(
            {
                "type": "tool_finished",
                "tool": call.name,
                "success": result.success,
            }
        )
        self.debug({
            "phase": "tool_result",
            "tool_call_id": call.id,
            "tool": call.name,
            "success": result.success,
            "content": _truncate(result.content, MAX_LOG_RESULT_CHARS),
            "image_count": len(result.images),
        })
        return result

    @staticmethod
    def _tool_result(call: ToolCall, success: bool, content: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id, name=call.name, success=success, content=content
        )

    def _failed(self, error: str) -> TaskResult:
        self.emit({"type": "task_failed", "error": error})
        return TaskResult(status="failed", error=error)

    @staticmethod
    def _publish(
        call: ToolCall, policy: WorkspacePolicy, output_dir: Path
    ) -> list[Path]:
        artifacts = call.arguments.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("finish_task requires at least one artifact")
        published: list[Path] = []
        for relative in artifacts:
            source = policy.require_file(str(relative))
            target = output_dir / source.name
            index = 2
            while target.exists():
                target = output_dir / f"{source.stem}-{index}{source.suffix}"
                index += 1
            shutil.copy2(source, target)
            published.append(target)
        return published

    @staticmethod
    @staticmethod
    def _stage_materials(
        workspace: Path, materials: list[Path]
    ) -> list[tuple[str, Path]]:
        if not materials:
            return []
        target = workspace / "sources"
        target.mkdir(exist_ok=True)
        staged: list[tuple[str, Path]] = []
        for src in materials:
            if src.is_file():
                shutil.copy2(src, target / src.name)
                staged.append((f"sources/{src.name}", target / src.name))
        return staged
