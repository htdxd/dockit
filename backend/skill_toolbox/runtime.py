"""应用任务入口；循环与简历领域分别维护，不再携带 legacy 工具路径。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from skill_toolbox.agent_loop import redact_debug, run_loop
from skill_toolbox.materials import MaterialError
from skill_toolbox.providers.base import ModelProvider
from skill_toolbox.resume_context import current_resume_context
from skill_toolbox.resume_task import ResumeTask
from skill_toolbox.runtime_types import TaskRequest, TaskResult, UserInputBroker
from skill_toolbox.skills import load_skill, RETIRED_PDF_IDS, FEATURE_RETIRED_MESSAGE
from skill_toolbox.tools.resume_workflow import SUPPORTED_TEMPLATES
from skill_toolbox.unicode_utils import redact_secrets

__all__ = ["AgentRuntime", "TaskRequest", "TaskResult", "UserInputBroker"]


class AgentRuntime:
    def __init__(
        self,
        provider: ModelProvider,
        emit,
        input_broker=None,
        model_timeout_seconds=1200.0,
        tool_timeout_seconds=90.0,
        debug_logger=None,
    ):
        self.provider, self.emit, self.input_broker = provider, emit, input_broker
        self.model_timeout_seconds, self.tool_timeout_seconds = (
            model_timeout_seconds,
            tool_timeout_seconds,
        )
        sink = debug_logger or (lambda entry: None)
        self.debug = lambda entry: sink(redact_debug(entry))

    async def run(self, request: TaskRequest) -> TaskResult:
        error = None
        if request.skill_id in RETIRED_PDF_IDS:
            error = FEATURE_RETIRED_MESSAGE
        elif request.tool_mode not in (None, "domain"):
            error = "[FEATURE_RETIRED] legacy 通用工具模式已下线，请使用简历工作流。"
        elif request.template_id and request.template_id not in SUPPORTED_TEMPLATES:
            error = "TEMPLATE_UNSUPPORTED: 当前仅支持已组件化的模板。"
        if error:
            return self._complete(TaskResult("failed", error=error))
        skill = load_skill(request.skill_id)
        self.emit({"type": "task_started", "skill_id": skill.id})
        self.debug(
            {
                "phase": "task_start",
                "skill_id": skill.id,
                "user_prompt": request.user_prompt,
                "writing_style": request.writing_style,
                "output_dir": str(request.output_dir),
                "materials": [str(p) for p in request.materials],
                "capabilities": request.capabilities,
                "provider": type(self.provider).__name__,
                "model": getattr(self.provider, "model", None),
                "max_tokens": getattr(self.provider, "max_tokens", None),
                "reasoning_level": getattr(self.provider, "reasoning_level", None),
            }
        )
        with tempfile.TemporaryDirectory(prefix="skill-toolbox-") as temp:
            task = ResumeTask(
                Path(temp),
                skill,
                request,
                self.provider,
                self.emit,
                self.debug,
                self.input_broker,
                self.model_timeout_seconds,
                self.tool_timeout_seconds,
            )
            try:
                prompt, messages, tools = await task.prepare()
                result = await run_loop(
                    self.provider,
                    prompt,
                    messages,
                    tools,
                    max_steps=skill.max_steps,
                    model_timeout=self.model_timeout_seconds,
                    emit=self.emit,
                    debug=self.debug,
                    cancel=task.cancel,
                    transform_context=current_resume_context,
                    after_tools=task.after_tools,
                )
                if result.status != "completed":
                    task.emit_qa()
                return self._complete(result)
            except MaterialError as exc:
                return self._complete(TaskResult("failed", error=f"[{exc.code}] {exc}"))
            except asyncio.CancelledError:
                raise
            finally:
                task.cancel(True)

    def _complete(self, result):
        if result.status == "completed":
            self.emit(
                {
                    "type": "task_completed",
                    "artifacts": [str(p) for p in result.artifacts],
                }
            )
        else:
            self.emit(
                {
                    "type": "task_failed",
                    "error": redact_secrets(result.error or "Task failed"),
                }
            )
        return result
