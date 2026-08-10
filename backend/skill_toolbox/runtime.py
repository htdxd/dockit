from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skill_toolbox.materials import MaterialCatalog, MaterialError, MaterialService
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
# 外层超时 = 声明的 action 超时 + 清理余量（给子进程退出留时间）。
ACTION_TIMEOUT_CLEANUP = 30


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
    # Resolved model capabilities (e.g. {"vision": True}) injected as a banner
    # at the top of the skill's system prompt so prompt-level routing can
    # depend on them without guessing.
    capabilities: dict[str, bool] = field(default_factory=dict)
    # Environment variables injected into exec_cmd subprocesses (e.g.
    # MINERU_TOKEN for the PDF skill's MinerU scripts).
    env: dict[str, str] = field(default_factory=dict)


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
        model_timeout_seconds: float = 1200.0,
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
        self.skill_id = skill.id  # _publish 用它给产物文件名打来源标记
        request.output_dir.mkdir(parents=True, exist_ok=True)
        system_prompt = skill.system_prompt
        if request.capabilities:
            banner = "[CAPABILITIES]\n" + "\n".join(
                f"{name}: {str(value).lower()}"
                for name, value in sorted(request.capabilities.items())
            )
            system_prompt = f"{banner}\n\n{system_prompt}"
        self.emit({"type": "task_started", "skill_id": skill.id})
        self.debug({
            "phase": "task_start",
            "skill_id": skill.id,
            "user_prompt": request.user_prompt,
            "output_dir": str(request.output_dir),
            "materials": [str(p) for p in request.materials],
            "capabilities": request.capabilities,
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
            # 阶段 3：进入 Agent loop 前同步预处理材料（md/txt 原生解析为
            # DocumentIR；pdf/docx/pptx 登记占位，旧 ingest/read 路径继续服务）。
            # 横幅不再自动 ingest（实施计划 §12.1：横幅只读预处理结果短摘要）。
            material_service = MaterialService(workspace, request.env)
            material_errors: list[tuple[str, str, str]] = []
            for rel, abs_path in staged:
                self.emit({"type": "material_progress", "path": rel, "phase": "parsing"})
                try:
                    material_service.prepare_material(abs_path)
                except MaterialError as exc:
                    material_errors.append((rel, exc.code, str(exc)))
                    self.emit(
                        {
                            "type": "material_progress",
                            "path": rel,
                            "phase": "failed",
                            "error": exc.code,
                        }
                    )
                    continue
                self.emit({"type": "material_progress", "path": rel, "phase": "done"})
            catalog = material_service.catalog()
            policy = WorkspacePolicy(workspace, read_roots=(skill.dir,))
            tools = ToolRegistry(
                policy,
                skill_dir=skill.dir,
                scripts=skill.scripts,
                env=request.env,
                script_timeouts=skill.script_timeouts,
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
            materials_banner = self._materials_banner(catalog, material_errors)
            if materials_banner:
                system_prompt = f"{materials_banner}\n\n{system_prompt}"
            messages.append(ConversationMessage(role="user", text=user_text))
            text_only_turns = 0
            for step in range(1, skill.max_steps + 1):
                self.emit({"type": "model_started", "step": step})
                try:
                    turn = await asyncio.wait_for(
                        self.provider.complete(system_prompt, messages, TOOL_SPECS),
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
                        return self._failed(
                            "Model produced two consecutive turns with no tool calls."
                        )
                    messages.append(
                        ConversationMessage(
                            role="user",
                            text=(
                                "你上一轮没有调用任何工具，任务无法推进。"
                                "如果最终产物已经生成并验证完毕，请立即调用 "
                                "finish_task 交付（只传产物相对路径）；否则请调用 "
                                "read/write/edit/exec_cmd/ask_user_questions 继续。"
                            ),
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
                abort = next(
                    (call for call in turn.tool_calls if call.name == "task_failed"),
                    None,
                )
                if abort is not None and len(turn.tool_calls) == 1:
                    # 模型显式放弃：失败必须走 task_failed，禁止用 finish_task
                    # 伪造说明型 artifact（实施计划 §9.5 / §10.3）。
                    error = str(abort.arguments.get("error", "")).strip()
                    self.emit({"type": "tool_started", "tool": abort.name})
                    self.emit(
                        {"type": "tool_finished", "tool": abort.name, "success": True}
                    )
                    return self._failed(error or "Task aborted by the model")
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

    def _timeout_for(self, call: ToolCall, tools: ToolRegistry) -> float:
        """单次工具调用的外层超时。

        普通工具沿用默认短超时；exec_cmd 若在 manifest script spec 声明了
        `timeout_seconds`（MinerU/Office 长任务），外层必须覆盖脚本内部超时
        再加清理余量，否则外层 90s 会提前终止内部 1800s 的 MinerU
        （实施计划 §9.5）。未声明的 action 返回默认值。
        """
        if call.name == "exec_cmd":
            action = str(call.arguments.get("action", ""))
            declared = tools.action_timeout(action)
            if declared > 0:
                return max(self.tool_timeout_seconds, declared + ACTION_TIMEOUT_CLEANUP)
        return self.tool_timeout_seconds

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
        elif call.name == "task_failed":
            result = self._tool_result(
                call, False, "task_failed must be the only tool call in its model turn"
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
                    tools.execute(call), timeout=self._timeout_for(call, tools)
                )
            except TimeoutError:
                result = self._tool_result(
                    call,
                    False,
                    f"Tool timed out after {self._timeout_for(call, tools):g} seconds",
                )
                self.emit(
                    {
                        "type": "tool_timeout",
                        "tool": call.name,
                        "timeout_seconds": self._timeout_for(call, tools),
                    }
                )
                self.debug({
                    "phase": "tool_result",
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "success": False,
                    "error": f"timed out after {self._timeout_for(call, tools):g}s",
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

    @staticmethod
    def _failed_event(error: str) -> dict[str, str]:
        """稳定错误码 / 结构化失败事件（见实施计划 §9.1）。"""
        return {"type": "task_failed", "error": error}

    def _failed(self, error: str) -> TaskResult:
        self.emit(self._failed_event(error))
        return TaskResult(status="failed", error=error)

    def _publish(
        self, call: ToolCall, policy: WorkspacePolicy, output_dir: Path
    ) -> list[Path]:
        artifacts = call.arguments.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("finish_task requires at least one artifact")
        published: list[Path] = []
        for relative in artifacts:
            source = policy.require_file(str(relative))
            # 文件名带 skill 来源标记（如 resume.resume_pro.docx），前端按标记
            # 把产物归到产生它的功能页，避免按扩展名串检。同名去重仍保留，
            # 去重后缀插在 stem 与标记之间，标记不丢（resume-2.resume_pro.docx）。
            marker = f".{self.skill_id}"
            target = output_dir / f"{source.stem}{marker}{source.suffix}"
            index = 2
            while target.exists():
                target = output_dir / f"{source.stem}-{index}{marker}{source.suffix}"
                index += 1
            shutil.copy2(source, target)
            published.append(target)
        return published

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

    def _materials_banner(
        self,
        catalog: MaterialCatalog,
        material_errors: list[tuple[str, str, str]],
    ) -> str:
        """生成 [MATERIALS] 横幅：只读预处理结果短摘要，不执行 ingest。

        阶段 3 起横幅不再自动萃取（实施计划 §12.1）。md/txt 已解析为
        DocumentIR（列出 document.json/content.md/资源数）；pdf/docx/pptx
        登记占位，引导模型用 read/ingest 获取富内容；解析失败列出稳定错误码。
        """
        if not catalog.manifest["materials"] and not material_errors:
            return ""
        lines: list[str] = ["[MATERIALS]"]
        for entry in catalog.manifest["materials"]:
            material_id = entry["material_id"]
            ir = catalog.irs.get(material_id)
            if ir is None:
                lines.append(f"- {entry['original_names'][0]}（解析失败）")
                continue
            if not ir.blocks and ir.warnings and "尚未接入共享层" in " ".join(ir.warnings):
                lines.append(
                    f"- {entry['original_names'][0]}（{ir.source_format}）→ 工作区已登记；"
                    f"用 read/ingest 获取富内容（含图片/表格/公式）"
                )
                continue
            lines.append(
                f"- {entry['original_names'][0]}（{ir.source_format}, {len(ir.blocks)} 块, "
                f"{len(ir.assets)} 资源）→ work/materials/{material_id[:16]}/document.json，"
                f"顺序阅读 work/materials/{material_id[:16]}/content.md"
            )
        for rel, code, message in material_errors:
            lines.append(f"- {rel} 材料处理失败（{code}）: {message}")
        return "\n".join(lines)

