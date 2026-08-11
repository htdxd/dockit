from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skill_toolbox.material_models import ContentPlan, QAReport
from skill_toolbox.materials import (
    MaterialCatalog,
    MaterialError,
    MaterialService,
    safe_stem,
    sha256_file,
)
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
TASK_TYPE_BY_SKILL = {
    "ppt-master": "ppt",
    "resume_pro": "resume",
    "docx_pro": "docx",
    "pdf_docx_routing": "pdf_to_docx",
}
DOCUMENT_QUALITY_PATH_ARG = {
    "postcheck_docx": "source",
    "audit_docx": "source",
    "fill_resume": "output",
}


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
    # Non-secret environment variables injected into declared exec_cmd scripts.
    env: dict[str, str] = field(default_factory=dict)
    # MinerU API Token（Sidecar 持有的受限凭据）。不进入 Agent 可见的任何
    # 路径：仅 MaterialService 的 PDF adapter 使用；所有 Agent exec_cmd
    # 子进程均拿不到（实施计划 §3.1/§12.1）。
    mineru_token: str | None = None


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
            # 阶段 3：进入 Agent loop 前预处理材料（md/txt 原生解析为
            # DocumentIR；pptx 复用 ppt-master parser；pdf 走 MinerU 共享解析；
            # docx 原生提取文本与媒体）。横幅不再自动 ingest（§12.1）。
            material_service = MaterialService(
                workspace, request.env, mineru_token=request.mineru_token
            )
            try:
                for rel, abs_path in staged:
                    self.emit({"type": "material_progress", "path": rel, "phase": "parsing"})
                    try:
                        # 预处理（含 MinerU/PPT 子进程）放到线程执行，避免阻塞
                        # Sidecar 事件循环；取消时由 terminate_all() 终止子进程
                        # （实施计划 §9.2/§9.5）。
                        await asyncio.to_thread(
                            material_service.prepare_material, abs_path
                        )
                    except MaterialError as exc:
                        self.emit(
                            {
                                "type": "material_progress",
                                "path": rel,
                                "phase": "failed",
                                "error": exc.code,
                            }
                        )
                        return self._failed(f"[{exc.code}] {exc}")
                    self.emit({"type": "material_progress", "path": rel, "phase": "done"})
            finally:
                # 取消/异常时终止仍活动的解析子进程（MinerU/PPT parser）
                material_service.terminate_all()
            catalog = material_service.catalog()
            policy = WorkspacePolicy(workspace, read_roots=(skill.dir,))
            tools = ToolRegistry(
                policy,
                skill_dir=skill.dir,
                scripts=skill.scripts,
                env=request.env,
                script_timeouts=skill.script_timeouts,
                capabilities=request.capabilities,
            )
            passed_quality_actions: set[str] = set()
            quality_hashes: dict[str, set[str]] = {}
            messages: list[ConversationMessage] = []
            user_text = request.user_prompt or "按默认内容生成测试文档"
            if staged:
                files_list = "\n".join(f"  - {rel}" for rel, _ in staged)
                user_text = (
                    f"{user_text}\n\n"
                    "你上传的原始文件已暂存到工作区，相对路径如下。内容读取以 "
                    "[MATERIALS] 中的共享 IR 为准；仅在声明脚本明确要求 source 时使用"
                    f"这些原始路径，不要再次 ingest：\n{files_list}"
                )
            materials_banner = self._materials_banner(catalog)
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
                        # 先读 QA：机械门是硬门，mechanical=failed 直接拒绝交付
                        # （不发布、不发 task_completed）。QAReport 由 Skill 脚本
                        # 写入 work/qa/*.json（§8.3）；缺报告视为未确认，同样拒绝。
                        # ContentPlan 是四个 Skill 的统一前置契约；即使没有上传
                        # 材料，也必须由 Agent 写出合法的空计划，避免无材料路径
                        # 绕过规划纪律（实施计划 §8.2/§15.3）。
                        self._load_content_plan(
                            workspace,
                            TASK_TYPE_BY_SKILL[skill.id],
                            request.capabilities,
                            catalog,
                        )
                        qa = self._load_qa_report(workspace, request.capabilities)
                        if qa.mechanical != "passed":
                            raise ValueError(
                                "机械检查未通过（mechanical=failed），不能交付。"
                                "请先修复机械检查问题或调用 task_failed 明确失败。"
                            )
                        missing_quality = skill.quality_actions - passed_quality_actions
                        if skill.quality_actions and not (
                            skill.quality_actions & passed_quality_actions
                        ):
                            raise ValueError(
                                "缺少可信机械检查结果：必须成功执行以下 action 之一后再交付："
                                + ", ".join(sorted(missing_quality))
                            )
                        self._verify_quality_artifacts(
                            finish,
                            policy,
                            skill.quality_actions,
                            quality_hashes,
                        )
                        published = self._publish(finish, policy, request.output_dir)
                        self.emit({"type": "qa_status", "qa": self._qa_event(qa)})
                    except (OSError, TypeError, ValueError) as exc:
                        # 机械门/QA 未过：不发 task_completed，把失败原因回给
                        # 模型继续修复（模型若无法修复应调用 task_failed 结束）。
                        result = self._tool_result(finish, False, str(exc))
                        self.emit(
                            {
                                "type": "tool_finished",
                                "tool": finish.name,
                                "success": False,
                            }
                        )
                        self.emit(
                            {
                                "type": "qa_status",
                                "qa": {
                                    "mechanical": "failed",
                                    "mechanical_issues": [str(exc)],
                                    "visual": "not_run",
                                    "visual_issues": [],
                                    "repair_rounds": 0,
                                    "used_assets": 0,
                                    "skipped_assets": 0,
                                },
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
                    self.emit(
                        {
                            "type": "qa_status",
                            "qa": {
                                "mechanical": "failed",
                                "mechanical_issues": [error or "Task aborted by the model"],
                                "visual": "not_run",
                                "visual_issues": [],
                                "repair_rounds": 0,
                                "used_assets": 0,
                                "skipped_assets": 0,
                            },
                        }
                    )
                    return self._failed(error or "Task aborted by the model")
                # 单轮工具调用中出现 task_failed 或 finish_task（多个工具并行）：
                # 按 tool_specs 契约，这两个工具必须是唯一调用。task_failed 出现
                # 即模型放弃任务——立即终止，不执行同轮其它调用（避免在放弃时
                # 仍跑 MinerU/Office 等昂贵动作）。finish_task 混入多调用按普通
                # 失败工具处理（"must be the only tool call"），模型据此修正。
                invalid = next(
                    (
                        call
                        for call in turn.tool_calls
                        if call.name in ("finish_task", "task_failed")
                    ),
                    None,
                )
                if invalid is not None:
                    if invalid.name == "task_failed":
                        # task_failed 是唯一合法终止信号：即使与其它调用同轮，
                        # 也按放弃处理，不执行同轮其它调用（Tool Spec 契约）。
                        error = str(invalid.arguments.get("error", "")).strip()
                        self.emit(
                            {
                                "type": "qa_status",
                                "qa": {
                                    "mechanical": "failed",
                                    "mechanical_issues": [error or "Task aborted by the model"],
                                    "visual": "not_run",
                                    "visual_issues": [],
                                    "repair_rounds": 0,
                                    "used_assets": 0,
                                    "skipped_assets": 0,
                                },
                            }
                        )
                        return self._failed(error or "Task aborted by the model")
                    # finish_task 混入多调用：按普通失败工具执行（执行链会返回
                    # "must be the only tool call"），模型据此修正。
                    results = await self._execute_calls(turn.tool_calls, tools)
                    self._record_quality_results(
                        turn.tool_calls,
                        results,
                        skill.quality_actions,
                        passed_quality_actions,
                        quality_hashes,
                        policy,
                    )
                    messages.append(
                        ConversationMessage(role="tool", tool_results=results)
                    )
                    continue
                results = await self._execute_calls(turn.tool_calls, tools)
                self._record_quality_results(
                    turn.tool_calls,
                    results,
                    skill.quality_actions,
                    passed_quality_actions,
                    quality_hashes,
                    policy,
                )
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
                self.emit(
                    {
                        "type": "questions_answered",
                        "count": len(questions),
                        "answer_ids": [str(q.get("id", "")) for q in questions],
                    }
                )
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
    def _load_content_plan(
        workspace: Path,
        task_type: str,
        capabilities: dict[str, bool],
        catalog: MaterialCatalog,
    ) -> ContentPlan:
        path = workspace / "work" / "plans" / "content-plan.json"
        if not path.is_file():
            raise ValueError("缺少 ContentPlan（work/plans/content-plan.json）")
        try:
            plan = ContentPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"ContentPlan 无效: {exc}") from None
        if plan.task_type != task_type:
            raise ValueError(
                f"ContentPlan.task_type 应为 {task_type}，实际为 {plan.task_type}"
            )
        if not capabilities.get("vision", False) and plan.mode != "conservative":
            raise ValueError("vision=false 时 ContentPlan.mode 必须为 conservative")

        selected = [item.source_id for item in plan.selections]
        excluded = [item.source_id for item in plan.exclusions]
        if len(selected) != len(set(selected)) or len(excluded) != len(set(excluded)):
            raise ValueError("ContentPlan 中同一 source_id 不能重复")
        overlap = set(selected) & set(excluded)
        if overlap:
            raise ValueError(f"ContentPlan 同时选择并排除了资源: {sorted(overlap)}")
        known_ids = {
            item.id
            for ir in catalog.irs.values()
            for item in [*ir.blocks, *ir.assets]
        }
        unknown = (set(selected) | set(excluded)) - known_ids
        if unknown:
            raise ValueError(f"ContentPlan 引用了未知 source_id: {sorted(unknown)}")
        asset_ids = {
            asset.id for ir in catalog.irs.values() for asset in ir.assets
        }
        missing_assets = asset_ids - set(selected) - set(excluded)
        if missing_assets:
            raise ValueError(
                "ContentPlan 必须选择或明确排除每个候选 Asset: "
                + ", ".join(sorted(missing_assets))
            )
        return plan

    @staticmethod
    def _load_qa_report(
        workspace: Path, capabilities: dict[str, bool] | None = None
    ) -> QAReport:
        """读取 Skill 写入的 QAReport（work/qa/*.json，§8.3）。

        按文件 mtime 取最新的一个并用共享 Pydantic schema 校验；缺失或非法
        都拒绝发布。机械通过仍需本轮可信 quality action 的成功证据。
        """
        qa_root = workspace / "work" / "qa"
        files = sorted(
            qa_root.glob("*.json"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        if not files:
            raise ValueError("缺少 QAReport（work/qa/*.json），不能确认质量状态")
        try:
            qa = QAReport.model_validate_json(files[0].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"QAReport 无效: {exc}") from None
        if qa.mechanical == "passed" and qa.mechanical_issues:
            raise ValueError("mechanical=passed 时 mechanical_issues 必须为空")
        vision = (capabilities or {}).get("vision", False)
        if not vision and qa.visual != "not_run":
            raise ValueError("vision=false 时 QAReport.visual 必须为 not_run")
        if vision and qa.visual != "passed":
            raise ValueError("vision=true 时 QAReport.visual 必须为 passed")
        return qa

    @staticmethod
    def _qa_event(qa: QAReport) -> dict[str, Any]:
        payload = qa.model_dump()
        payload["used_assets"] = len(qa.used_assets)
        payload["skipped_assets"] = len(qa.skipped_assets)
        return payload

    @staticmethod
    def _record_quality_results(
        calls: list[ToolCall],
        results: list[ToolResult],
        quality_actions: frozenset[str],
        passed: set[str],
        quality_hashes: dict[str, set[str]],
        policy: WorkspacePolicy,
    ) -> None:
        for call, result in zip(calls, results, strict=True):
            if call.name != "exec_cmd":
                continue
            action = str(call.arguments.get("action", ""))
            if action not in quality_actions:
                continue
            action_passed = result.success
            if action == "fill_resume" and action_passed:
                try:
                    outer = json.loads(result.content)
                    report = json.loads(outer.get("stdout", ""))
                    overflow_risks = report["overflow_risks"]
                    action_passed = (
                        report.get("ok") is True
                        and isinstance(overflow_risks, list)
                        and not overflow_risks
                    )
                except (AttributeError, KeyError, json.JSONDecodeError, TypeError):
                    action_passed = False
            path_arg = DOCUMENT_QUALITY_PATH_ARG.get(action)
            if action_passed and path_arg:
                try:
                    args = call.arguments.get("args", {})
                    checked = policy.require_file(str(args[path_arg]))
                    if checked.suffix.lower() != ".docx":
                        raise ValueError("quality action target is not DOCX")
                    checked_hash = sha256_file(checked)
                except (KeyError, OSError, TypeError, ValueError):
                    action_passed = False
                else:
                    quality_hashes.setdefault(action, set()).add(checked_hash)
            if action_passed:
                passed.add(action)
            else:
                passed.discard(action)
                quality_hashes.pop(action, None)

    @staticmethod
    def _verify_quality_artifacts(
        call: ToolCall,
        policy: WorkspacePolicy,
        quality_actions: frozenset[str],
        quality_hashes: dict[str, set[str]],
    ) -> None:
        document_actions = quality_actions & DOCUMENT_QUALITY_PATH_ARG.keys()
        if not document_actions:
            return
        artifacts = call.arguments.get("artifacts")
        if not isinstance(artifacts, list):
            raise TypeError("finish_task artifacts 必须为数组")
        docx_files = [
            (str(relative), policy.require_file(str(relative)))
            for relative in artifacts
            if Path(str(relative)).suffix.lower() == ".docx"
        ]
        if not docx_files:
            raise ValueError("当前功能必须交付至少一个经过机械检查的 DOCX")
        accepted = {
            digest
            for action in document_actions
            for digest in quality_hashes.get(action, set())
        }
        unchecked = [
            relative
            for relative, path in docx_files
            if sha256_file(path) not in accepted
        ]
        if unchecked:
            raise ValueError(
                "交付 DOCX 与最近一次成功机械检查的内容不一致: "
                + ", ".join(unchecked)
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
        workspace.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[str, Path]] = []
        for idx, src in enumerate(materials):
            if not src.is_file():
                continue
            # 按 basename 平铺会覆盖同名文件。每个材料独占子目录（序号 + 安全
            # stem 保证唯一），同名互不覆盖（实施计划 §9.2）。
            bucket = f"{idx}-{safe_stem(src.name) or 'material'}"
            target = workspace / "sources" / bucket
            target.mkdir(parents=True, exist_ok=True)
            copy = target / src.name
            shutil.copy2(src, copy)
            # staged 路径给 Agent 的 read/exec_cmd 用；prepare_material 拿到
            # 原始路径 src（md 相对引用以原文件父目录为基准解析，暂存目录形状
            # 不影响——materials.py 自行再暂存一份到 sources/<hash>/）。
            staged.append((f"sources/{bucket}/{src.name}", src))
        return staged

    def _materials_banner(self, catalog: MaterialCatalog) -> str:
        """生成 [MATERIALS] 横幅：只读预处理结果短摘要，不执行 ingest。

        阶段 3 起横幅不再自动萃取（实施计划 §12.1）。所有支持格式均列出
        document.json/content.md/资源数；解析失败已在进入 Agent loop 前终止。
        """
        if not catalog.manifest["materials"]:
            return ""
        lines: list[str] = ["[MATERIALS]"]
        for entry in catalog.manifest["materials"]:
            material_id = entry["material_id"]
            ir = catalog.irs.get(material_id)
            if ir is None:
                lines.append(f"- {entry['original_names'][0]}（解析失败）")
                continue
            prepared = (
                f"，准备产物 {str(entry['prepared_docx']).replace(chr(92), '/')}"
                if entry.get("prepared_docx")
                else ""
            )
            content_path = Path(catalog.compat_projection[material_id])
            content_rel = content_path.as_posix()
            document_rel = content_path.with_name("document.json").as_posix()
            lines.append(
                f"- {entry['original_names'][0]}（{ir.source_format}, {len(ir.blocks)} 块, "
                f"{len(ir.assets)} 资源）→ {document_rel}，"
                f"顺序阅读 {content_rel}{prepared}"
            )
        return "\n".join(lines)

