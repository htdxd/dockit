from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from skill_toolbox.material_models import ContentPlan, QAReport, SCHEMA_VERSION
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
from skill_toolbox.skills import load_skill, RETIRED_PDF_IDS, FEATURE_RETIRED_MESSAGE
from skill_toolbox.tool_specs import TOOL_SPECS
from skill_toolbox.tools import ToolRegistry

# 受控工具与 Skill 适配重构（实施计划）：领域工具（LLM-facing）与底层
# Service 分离。skill_defs 的 manifest 新增 "tool_mode": "domain" 时，Runtime
# 进入领域模式：LLM 只见领域 schema，工具调用走 llm_tools dispatcher →
# tools/ 内部 Service；旧 ToolRegistry 仍保留为兼容路径（普通模式默认）。
from skill_toolbox.llm_tools.common import shared_tools
from skill_toolbox.llm_tools.dispatcher import (  # noqa: E402
    DomainServices,
    dispatch,  # noqa: F401 - 兼容路径/测试
    dispatch_with_media,
)
from skill_toolbox.llm_tools.docx import docx_tools
from skill_toolbox.llm_tools.ppt import ppt_tools
from skill_toolbox.llm_tools.resume import resume_tools
from skill_toolbox.llm_tools.resume_workflow import workflow_tools
from skill_toolbox.tools.resume_workflow import ResumeWorkflow, SUPPORTED_TEMPLATES
from skill_toolbox.tools.docx import DocxService
from skill_toolbox.tools.materials import MaterialPlanService
from skill_toolbox.tools.ppt import PptService
from skill_toolbox.tools.resume import ResumeService
from skill_toolbox.unicode_utils import redact_secrets

EventEmitter = Callable[[dict[str, Any]], None]
DebugLogger = Callable[[dict[str, Any]], None]
MAX_PROGRESS_TEXT_CHARS = 2_000
MAX_LOG_ARG_CHARS = 2_000
MAX_LOG_RESULT_CHARS = 4_000
MAX_LOG_TEXT_CHARS = 2_000
# 外层超时 = 声明的 action 超时 + 清理余量（给子进程退出留时间）。
ACTION_TIMEOUT_CLEANUP = 30
DOMAIN_TOOL_TIMEOUTS = {
    "resume_generate": 420.0,
    "resume_edit": 420.0,
    "resume_repair": 210.0,
    # v2：一次调用含测量(Word COM)+布局+落盘+渲染(COM→PDF→PNG)+QA
    "resume_prepare_v2": 60.0,
    "resume_generate_v2": 420.0,
    "resume_repair_v2": 420.0,
    "resume_accept": 60.0,
    "resume_restore": 420.0,
    "resume_preview": 60.0,
    "docx_finalize": 330.0,
    "docx_repair": 330.0,
    "ppt_generate": 210.0,
    "ppt_repair": 210.0,
}
DOMAIN_ARTIFACT_TOOLS = frozenset(
    {
        "resume_generate",
        "resume_repair",
        "resume_edit",
        # v2（P2-3）：候选版本本身即登记产物；accept 只更新已接受指针，
        # 不产生新文件（避免绕过 hash 校验的“伪产物”）。
        "resume_generate_v2",
        "resume_repair_v2",
        "resume_restore",
        "docx_finalize",
        "docx_repair",
        "ppt_generate",
    }
)
PENDING_VISUAL_REVIEW = "pending-visual-review.json"
TASK_TYPE_BY_SKILL = {
    "ppt-master": "ppt",
    "resume_pro": "resume",
    "docx_pro": "docx",
}
DOCUMENT_QUALITY_PATH_ARG = {
    "postcheck_docx": "source",
    "fill_resume": "output",
}
# 领域工具 schema 路由（skill_id → 领域 schema 工厂）。普通任务不暴露
# write/edit/exec_cmd/spec_append 等底层工具（计划 §2.1/§5）。
_DOMAIN_TOOL_FACTORIES: dict[str, Callable[[], list[dict]]] = {
    "resume_pro": resume_tools,
    "docx_pro": docx_tools,
    "ppt-master": ppt_tools,
}


def _truncate(value: Any, limit: int) -> str:
    """Render any JSON-serialisable value to a truncated string for debug logs."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    text = redact_secrets(text)
    return text if len(text) <= limit else text[:limit] + f"…<+{len(text) - limit} bytes>"


def _redact_debug(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_debug(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_debug(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_debug(item) for item in value)
    return value


@dataclass(frozen=True)
class TaskRequest:
    skill_id: str
    user_prompt: str
    output_dir: Path
    materials: list[Path] = field(default_factory=list)
    template_id: str | None = field(default=None, kw_only=True)
    output_format: str | None = field(default=None, kw_only=True)
    writing_style: Literal["light", "balanced", "strong"] = field(default="balanced", kw_only=True)
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
    # 受控工具模式覆盖（"domain" 启用领域级 LLM tools，普通任务不暴露
    # write/edit/exec_cmd/spec_append——实施计划 §8）。None 时回落 Skill
    # manifest 的 tool_mode（默认 legacy，渐进迁移不破坏现有接口）。
    tool_mode: Literal["domain", "legacy"] | None = None


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
        sink = debug_logger or (lambda _entry: None)
        self.debug = lambda entry: sink(_redact_debug(entry))

    async def run(self, request: TaskRequest) -> TaskResult:
        if request.skill_id in RETIRED_PDF_IDS:
            self.emit({"type": "task_failed", "error": FEATURE_RETIRED_MESSAGE})
            return TaskResult(status="failed", error=FEATURE_RETIRED_MESSAGE)
        skill = load_skill(request.skill_id)
        if skill.id == "resume_pro" and request.template_id and request.template_id not in SUPPORTED_TEMPLATES:
            return self._failed("仅开放已组件化模板：t001、t109。请重新选择模板。")
        self.skill_id = skill.id  # _publish 用它给产物文件名打来源标记
        request.output_dir.mkdir(parents=True, exist_ok=True)
        system_prompt = skill.system_prompt
        workflow_mode = _resume_workflow_enabled(skill, request)
        if workflow_mode:
            system_prompt = (skill.dir / "workflow.md").read_text(encoding="utf-8")
        if skill.id == "resume_pro":
            from skill_toolbox.resume_facts import WRITING_STYLES
            system_prompt += "\n\n本次写作档位：" + WRITING_STYLES[request.writing_style]
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
            "writing_style": request.writing_style if skill.id == "resume_pro" else None,
            "output_dir": str(request.output_dir),
            "materials": [str(p) for p in request.materials],
            "capabilities": request.capabilities,
            "provider": type(self.provider).__name__,
            "model": getattr(self.provider, "model", None),
            "max_tokens": getattr(self.provider, "max_tokens", None),
            "reasoning_level": getattr(self.provider, "reasoning_level", None),
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
                material_service.terminate_active()
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
            # 领域模式（受控工具与 Skill 适配重构）：LLM 只见领域 schema，
            # 工具调用走 dispatcher → tools/ 内部 Service（计划 §3.1/§8）。
            # 有效模式 = TaskRequest.tool_mode 覆盖，否则 Skill manifest tool_mode
            # （默认 legacy，渐进迁移不破坏现有接口）。
            domain_mode = (request.tool_mode or skill.tool_mode) == "domain"
            domain_services = _build_domain_services(
                workspace,
                skill,
                catalog,
                request,
                material_service,
            )
            domain_tool_specs = _domain_tool_specs(skill, request.capabilities, workflow=workflow_mode)
            tool_specs = domain_tool_specs if domain_mode else TOOL_SPECS
            if domain_mode and not workflow_mode:
                # 领域模式（实施计划 §8）：注入领域工具指引，覆盖 prompt.md 中
                # 残留的底层工具说明——模型只用领域工具，不再 read/write/
                # exec_cmd/spec_append。用户 prompt.md 保持不动（兼容 legacy）。
                system_prompt = f"{system_prompt}\n\n{_DOMAIN_MODE_BANNER}"
            passed_quality_actions: set[str] = set()
            quality_hashes: dict[str, set[str]] = {}
            domain_artifacts: dict[str, str] = {}

            def validate_content_plan(_call: ToolCall) -> None:
                self._load_content_plan(
                    workspace,
                    TASK_TYPE_BY_SKILL[skill.id],
                    request.capabilities,
                    catalog,
                )

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
            materials_banner = self._materials_banner(catalog, domain_mode=domain_mode, workflow=workflow_mode)
            if materials_banner:
                system_prompt = f"{materials_banner}\n\n{system_prompt}"
            messages.append(ConversationMessage(role="user", text=user_text))
            text_only_turns = 0
            for step in range(1, skill.max_steps + 1):
                self.emit({"type": "model_started", "step": step})
                model_started = time.monotonic()
                model_messages = messages
                if workflow_mode:
                    from skill_toolbox.resume_context import current_resume_context
                    model_messages = current_resume_context(messages)
                try:
                    turn = await asyncio.wait_for(
                        self.provider.complete(system_prompt, model_messages, tool_specs),
                        timeout=self.model_timeout_seconds,
                    )
                except TimeoutError:
                    return self._failed(
                        f"LLM request timed out after {self.model_timeout_seconds:g} seconds"
                    )
                except Exception as exc:
                    causes = []
                    cause = exc
                    for _ in range(6):
                        if cause is None:
                            break
                        causes.append({"type": type(cause).__name__, "errno": getattr(cause, "errno", None)})
                        cause = cause.__cause__ or cause.__context__
                    self.debug({"phase": "model_error", "step": step,
                                "error": redact_secrets(str(exc))[:400], "causes": causes,
                                "elapsed_seconds": round(time.monotonic() - model_started, 3)})
                    raise
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
                    "response_metadata": getattr(turn, "response_metadata", {}),
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
                        role="assistant", text=turn.text, tool_calls=turn.tool_calls,
                        provider_items=turn.provider_items,
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
                        if turn.response_metadata.get("finish_reason") == "length":
                            return self._failed(
                                "模型在生成工具调用前耗尽了输出预算，重试后仍被截断。"
                                "请降低推理深度（建议中或高）后重试；强制工具调用无法突破输出上限。"
                            )
                        return self._failed(
                            "模型连续两轮未返回工具调用。请确认当前供应商和模型支持工具调用。"
                        )
                    messages.append(
                        ConversationMessage(
                            role="user",
                            text=(
                                (
                                    "上一轮达到输出 token 上限，未产生工具调用。请缩短推理，先执行一个最小必要步骤。"
                                    if turn.response_metadata.get("finish_reason") == "length"
                                    else "你上一轮没有调用任何工具，任务无法推进。"
                                ) +
                                "如果最终产物已经生成并验证完毕，请立即调用 "
                                "finish_task 交付（只传产物相对路径）；否则请调用 "
                                + (
                                    "resume_prepare/resume_generate/resume_edit/resume_preview 继续；"
                                    "完成检查后 resume_accept，再 finish_task。只有缺少必要事实时才补问，"
                                    "不要询问用户是否满意或把草稿交给用户验收。"
                                    if workflow_mode else
                                    "read_material/create_content_plan/本功能领域工具/"
                                    "ask_user_questions 继续。"
                                    if domain_mode
                                    else "read/write/edit/exec_cmd/ask_user_questions 继续。"
                                )
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
                        qa = self._load_qa_report(
                            workspace,
                            request.capabilities,
                            allow_visual_not_run=domain_mode,
                        )
                        if qa.mechanical != "passed":
                            raise ValueError(
                                "机械检查未通过（mechanical=failed），不能交付。"
                                "请先修复机械检查问题或调用 task_failed 明确失败。"
                            )
                        if not domain_mode:
                            # legacy 模式：必须存在本轮成功的机械质量 action 证据，
                            # 且交付 DOCX hash 与最近一次机械检查一致（§8.3/§15.3）。
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
                        else:
                            self._verify_domain_artifacts(
                                finish, policy, domain_artifacts
                            )
                        if workflow_mode:
                            domain_services.resume_workflow.verify_delivery(finish.arguments["artifacts"])
                        delivery = finish
                        if workflow_mode and request.output_format in {"DOCX", "PDF"}:
                            selected = [p for p in finish.arguments["artifacts"]
                                        if Path(p).suffix.lower() == "." + request.output_format.lower()]
                            if not selected:
                                raise ValueError(f"请交付用户选择的 {request.output_format} 文件。")
                            delivery = finish.model_copy(update={"arguments": {**finish.arguments, "artifacts": selected}})
                        published = self._publish(delivery, policy, request.output_dir)
                        self._emit_qa_status(qa)
                    except (OSError, TypeError, ValueError) as exc:
                        # 机械门/QA 未过：不发 task_completed，把失败原因回给
                        # 模型继续修复（模型若无法修复应调用 task_failed 结束）。
                        safe_error = redact_secrets(str(exc))
                        result = self._tool_result(finish, False, safe_error)
                        self.emit(
                            {
                                "type": "tool_finished",
                                "tool": finish.name,
                                "success": False,
                            }
                        )
                        self.debug({"phase": "finish_rejected", "error": safe_error})
                        self._emit_latest_qa_status(workspace)
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
                    self._emit_latest_qa_status(workspace)
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
                        self._emit_latest_qa_status(workspace)
                        return self._failed(error or "Task aborted by the model")
                    # finish_task 混入多调用：按普通失败工具执行（执行链会返回
                    # "must be the only tool call"），模型据此修正。
                    results = await self._execute_calls(
                        turn.tool_calls, tools, validate_content_plan, domain_services
                    )
                    self._record_quality_results(
                        turn.tool_calls,
                        results,
                        skill.quality_actions,
                        passed_quality_actions,
                        quality_hashes,
                        policy,
                    )
                    if domain_mode:
                        self._record_domain_artifacts(
                            turn.tool_calls, results, policy, domain_artifacts
                        )
                    messages.append(
                        ConversationMessage(role="tool", tool_results=results)
                    )
                    continue
                results = await self._execute_calls(
                    turn.tool_calls, tools, validate_content_plan, domain_services
                )
                self._record_quality_results(
                    turn.tool_calls,
                    results,
                    skill.quality_actions,
                    passed_quality_actions,
                    quality_hashes,
                    policy,
                )
                if domain_mode:
                    self._record_domain_artifacts(
                        turn.tool_calls, results, policy, domain_artifacts
                    )
                messages.append(ConversationMessage(role="tool", tool_results=results))
            self._emit_latest_qa_status(workspace)
            return self._failed(f"Task exceeded the {skill.max_steps}-step limit")

    async def _execute_calls(
        self,
        calls: list[ToolCall],
        tools: ToolRegistry,
        precheck: Callable[[ToolCall], None] | None = None,
        domain_services: DomainServices | None = None,
    ) -> list[ToolResult]:
        if all(call.name == "read" for call in calls):
            return await asyncio.gather(
                *(self._execute_call(call, tools, precheck, domain_services) for call in calls)
            )
        results: list[ToolResult] = []
        for call in calls:
            results.append(await self._execute_call(call, tools, precheck, domain_services))
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

    def _domain_timeout_for(self, call: ToolCall) -> float:
        declared = DOMAIN_TOOL_TIMEOUTS.get(call.name, 0.0)
        return max(self.tool_timeout_seconds, declared)

    async def _execute_call(
        self,
        call: ToolCall,
        tools: ToolRegistry,
        precheck: Callable[[ToolCall], None] | None = None,
        domain_services: DomainServices | None = None,
    ) -> ToolResult:
        self.emit({"type": "tool_started", "tool": call.name})
        self.debug({
            "phase": "tool_call",
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": _truncate(call.arguments, MAX_LOG_ARG_CHARS),
        })
        # 领域模式：领域工具走 dispatcher → 内部 Service（typed 参数校验在
        # contracts 层完成）；exec_cmd 前置 ContentPlan 校验仅 legacy 需要。
        # P2-3：dispatcher 同时返回内联预览图，由 ToolResult.images 进入模型
        # 视觉输入（附件关联 tool_call / artifact / revision）。
        if domain_services is not None and call.name in _DOMAIN_TOOL_NAMES:
            timeout = self._domain_timeout_for(call)
            try:
                content, images, media_meta = await asyncio.wait_for(
                    asyncio.to_thread(
                        dispatch_with_media, call.name, dict(call.arguments), domain_services
                    ),
                    timeout=timeout,
                )
                success = bool(media_meta.get("ok", False))
                result = self._tool_result(call, success, content, images)
                if media_meta.get("revision") is not None or media_meta.get("preview_refs"):
                    self.emit({
                        "type": "resume_preview",
                        "tool": call.name,
                        "revision": media_meta.get("revision"),
                        "preview_refs": media_meta.get("preview_refs", []),
                        "image_count": len(images),
                    })
            except TimeoutError:
                domain_services.cancel_all()
                result = self._tool_result(
                    call, False, f"Tool timed out after {timeout:g} seconds"
                )
                self.emit(
                    {
                        "type": "tool_timeout",
                        "tool": call.name,
                        "timeout_seconds": timeout,
                    }
                )
            except asyncio.CancelledError:
                domain_services.cancel_all(permanent=True)
                raise
            except Exception as exc:  # noqa: BLE001 - keep one bad tool from killing the task
                result = self._tool_result(
                    call, False, f"Tool failed: {redact_secrets(str(exc))}"
                )
            return self._finish_tool(call, result)
        if call.name == "exec_cmd" and precheck is not None:
            try:
                precheck(call)
            except (OSError, TypeError, ValueError) as exc:
                result = self._tool_result(
                    call, False, f"ContentPlan 前置校验失败，exec_cmd 未执行: {exc}"
                )
                return self._finish_tool(call, result)
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
                if domain_services is not None and domain_services.resume_workflow is not None:
                    source_id = domain_services.resume_workflow.facts.answer(questions, answers)
                    answers = {"answers": answers, "source_id": source_id}
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
                safe_error = redact_secrets(str(exc))
                result = self._tool_result(call, False, f"Tool failed: {safe_error}")
                self.emit({"type": "tool_failed", "tool": call.name, "error": safe_error})
                self.debug({
                    "phase": "tool_result",
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "success": False,
                    "error": f"{type(exc).__name__}: {safe_error}",
                })
                return result
        return self._finish_tool(call, result)

    def _finish_tool(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """统一工具完成事件和摘要日志，媒体内容不写入日志。"""
        self.emit(
            {
                "type": "tool_finished",
                "tool": call.name,
                "success": result.success,
            }
        )
        summary = {}
        if call.name in _DOMAIN_TOOL_NAMES:
            try:
                payload = json.loads(result.content)
                data = payload.get("data", {})
                summary = {"error_code": payload.get("code"),
                           "candidate_id": data.get("candidate_id"),
                           "page_count": data.get("page_count"),
                           "fits_page_target": data.get("fits_page_target")}
            except (ValueError, AttributeError):
                pass
        self.debug({
            "phase": "tool_result",
            "tool_call_id": call.id,
            "tool": call.name,
            "success": result.success,
            "content": _truncate(result.content, MAX_LOG_RESULT_CHARS),
            "image_count": len(result.images),
            "summary": summary,
        })
        return result

    @staticmethod
    def _tool_result(
        call: ToolCall,
        success: bool,
        content: str,
        images: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        from skill_toolbox.models import ImageContent

        payload: list[ImageContent] = []
        for img in images or []:
            media_type = str(img.get("media_type", "")) if isinstance(img, dict) else ""
            data = str(img.get("base64_data", "")) if isinstance(img, dict) else ""
            if media_type and data:
                payload.append(ImageContent(media_type=media_type, base64_data=data))
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            success=success,
            content=content,
            images=payload,
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
            # 模型常把计划写到 work/ 下的错误文件名/目录（content-plan.json、
            # content_plan.json、work/plans/ 缺失等），检测并直接提示挽救路径，
            # 避免"缺少 ContentPlan"反复往返烧步。
            candidates = [
                workspace / "work" / "content-plan.json",
                workspace / "work" / "content_plan.json",
                workspace / "work" / "plans" / "content_plan.json",
            ]
            found = [str(p.relative_to(workspace)) for p in candidates if p.is_file()]
            hint = (
                f"；检测到疑似写错位置的计划文件: {found}，请用 write 重写到 "
                "work/plans/content-plan.json"
                if found
                else ""
            )
            raise ValueError(
                "缺少 ContentPlan（必须写到 work/plans/content-plan.json，"
                f"目录 work/plans/ 且文件名 content-plan.json）{hint}"
            )
        # 宽松化：task_type/mode/schema_version 由任务上下文自动派生，模型无需填写。
        # 先读 dict 补派生字段再校验，其余语义校验（枚举/重复/未知 id/资源覆盖）不变。
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"ContentPlan 不是合法 JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise ValueError("ContentPlan 顶层必须是 JSON 对象")
        raw.setdefault("task_type", task_type)
        raw.setdefault(
            "mode", "vision" if capabilities.get("vision", False) else "conservative"
        )
        raw["schema_version"] = SCHEMA_VERSION
        try:
            plan = ContentPlan.model_validate(raw)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"ContentPlan 无效: {exc}；请重写 work/plans/content-plan.json "
                "(只需填写 selections/exclusions 的 source_id，其余字段后端自动补充；"
                "selections[].transform 只能是 preserve|summarize|crop|table|formula，"
                "exclusions[].reason 只能是 "
                "irrelevant|duplicate|low_confidence|unsupported|user_rejected，"
                "两者不要混淆)"
            ) from None
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
            raise ValueError(
                f"ContentPlan 引用了未知 source_id: {sorted(unknown)}。"
                "真实 source_id 必须从 work/materials/<id>/document.json 的 "
                "blocks[].id（形如 block-<材料id16>-<序号>）与 assets[].id"
                "（形如 asset-<材料id16>-<hash>）复制，禁止自造/改写前缀。"
            )
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
    def _read_qa_report(workspace: Path) -> QAReport:
        """读取 Skill 写入的 QAReport（work/qa/*.json，§8.3）。

        按文件 mtime 取最新的一个并用共享 Pydantic schema 校验；缺失或非法
        都拒绝发布。机械通过仍需本轮可信 quality action 的成功证据。
        """
        qa_root = workspace / "work" / "qa"
        files = sorted(
            (
                path
                for path in qa_root.glob("*.json")
                if path.name != PENDING_VISUAL_REVIEW
            ),
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
        return qa

    @staticmethod
    def _load_qa_report(
        workspace: Path,
        capabilities: dict[str, bool] | None = None,
        *,
        allow_visual_not_run: bool = False,
    ) -> QAReport:
        qa = AgentRuntime._read_qa_report(workspace)
        vision = (capabilities or {}).get("vision", False)
        if not vision and qa.visual != "not_run":
            raise ValueError("vision=false 时 QAReport.visual 必须为 not_run")
        if vision and qa.visual != "passed" and not allow_visual_not_run:
            raise ValueError("vision=true 时 QAReport.visual 必须为 passed")
        return qa

    @staticmethod
    def _qa_event(qa: QAReport) -> dict[str, Any]:
        payload = qa.model_dump()
        payload["used_assets"] = len(qa.used_assets)
        payload["skipped_assets"] = len(qa.skipped_assets)
        return payload

    def _emit_qa_status(self, qa: QAReport) -> None:
        payload = self._qa_event(qa)
        self.emit({"type": "qa_status", "qa": payload})
        self.debug({"phase": "qa_status", "qa": payload})

    def _emit_latest_qa_status(self, workspace: Path) -> None:
        try:
            qa = self._read_qa_report(workspace)
        except (OSError, ValueError):
            return
        self._emit_qa_status(qa)

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
    def _record_domain_artifacts(
        calls: list[ToolCall],
        results: list[ToolResult],
        policy: WorkspacePolicy,
        registered: dict[str, str],
    ) -> None:
        """登记领域 Service 本轮机械门通过后的真实产物路径与内容 hash。"""
        for call, result in zip(calls, results, strict=True):
            if call.name not in DOMAIN_ARTIFACT_TOOLS or not result.success:
                continue
            try:
                payload = json.loads(result.content)
                data = payload["data"]
                # v2 返回候选版本的 docx/pdf（多产物）；legacy 返回单个 path
                relative = str(
                    data.get("path")
                    or data.get("docx")
                    or data.get("pdf")
                    or ""
                )
                if not relative:
                    continue
                artifact = policy.require_file(relative)
                normalized = artifact.relative_to(policy.root).as_posix()
                registered[normalized] = sha256_file(artifact)
                pdf_rel = str(data.get("pdf") or "")
                if pdf_rel and pdf_rel != relative:
                    pdf_artifact = policy.require_file(pdf_rel)
                    registered[pdf_artifact.relative_to(policy.root).as_posix()] = (
                        sha256_file(pdf_artifact)
                    )
            except (KeyError, OSError, TypeError, ValueError):
                # 返回契约不完整时不登记；finish 门会给出明确拒绝。
                continue

    @staticmethod
    def _verify_domain_artifacts(
        call: ToolCall,
        policy: WorkspacePolicy,
        registered: dict[str, str],
    ) -> None:
        artifacts = call.arguments.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("finish_task requires at least one artifact")
        for relative in artifacts:
            artifact = policy.require_file(str(relative))
            normalized = artifact.relative_to(policy.root).as_posix()
            expected = registered.get(normalized)
            if expected is None:
                raise ValueError(
                    f"产物未由本轮成功的领域生成工具登记: {normalized}"
                )
            if sha256_file(artifact) != expected:
                raise ValueError(f"领域产物登记后已被修改，必须重新生成或修复: {normalized}")

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

    def _materials_banner(self, catalog: MaterialCatalog, *, domain_mode: bool = False, workflow: bool = False) -> str:
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
            if workflow:
                lines.append(f"  material_id={material_id}；先 resume_prepare 获取全文及来源 ID，无需另行读正文。")
            elif domain_mode:
                lines.append(
                    f"  整篇读取：read_material(source_id=\"{material_id}\", "
                    'view="blocks")；返回真实 block/asset ID，可按需继续读取。'
                )
            for asset in ir.assets:
                size = (
                    f"{asset.width}x{asset.height}"
                    if asset.width is not None and asset.height is not None
                    else "未知尺寸"
                )
                lines.append(
                    f"  候选 Asset: id={asset.id}; path="
                    f"{asset.path.replace(chr(92), '/')}; "
                    f"type={asset.mime_type}; size={size}"
                )
        return "\n".join(lines)



# ---------------- 受控工具与 Skill 适配重构：领域模式装配（实施计划 §8） ----------------
#
# skill.tool_mode == "domain" 时，Runtime 构造领域 Service 容器并按 Skill 组装
# 领域 schema；工具调用由 _execute_call 走 llm_tools.dispatcher → tools/ 内部
# Service。旧 ToolRegistry/TOOL_SPECS 保留为兼容路径（tool_mode 默认 legacy）。

# 以可见 schema 为准；交互和任务结束由 Runtime 自身处理。
_DOMAIN_TOOL_NAMES = frozenset(
    spec["name"]
    for factory in (shared_tools, workflow_tools, *_DOMAIN_TOOL_FACTORIES.values())
    for spec in factory()
) - {"ask_user_questions", "finish_task", "task_failed"}


def _resume_workflow_enabled(skill: Any, request: TaskRequest) -> bool:
    return (skill.id == "resume_pro" and request.template_id in SUPPORTED_TEMPLATES
            and (request.tool_mode or skill.tool_mode) == "domain")


def _domain_tool_specs(skill: Any, capabilities: dict[str, bool], *, workflow: bool = False) -> list[dict]:
    """按 Skill 组装领域工具 schema（shared + 领域）。普通任务不暴露底层工具。"""
    specs: list[dict] = list(shared_tools())
    if workflow:
        return [s for s in specs if s["name"] != "create_content_plan"] + workflow_tools()
    factory = _DOMAIN_TOOL_FACTORIES.get(skill.id)
    if factory is not None:
        specs.extend(factory())
    return specs


def _build_domain_services(
    workspace: Path,
    skill: Any,
    catalog: MaterialCatalog,
    request: TaskRequest,
    material_service: MaterialService,
) -> DomainServices:
    """按任务装配领域服务；共享材料服务始终可用。"""
    capabilities = request.capabilities
    materials = MaterialPlanService(
        workspace, catalog, TASK_TYPE_BY_SKILL[skill.id], capabilities
    )
    skill_dir = skill.dir
    resume = ResumeService(
        workspace,
        skill_dir / "templates",
        catalog=catalog,
        capabilities=capabilities,
    ) if skill.id == "resume_pro" else None
    # v2（P2-3）：版本化编辑服务与 legacy 并存；dispatcher 按请求形态分流
    if skill.id == "resume_pro":
        from skill_toolbox.tools.resume import ResumeEditService

        resume_v2 = ResumeEditService(
            workspace, skill_dir / "templates", capabilities=capabilities,
            template_id=request.template_id if _resume_workflow_enabled(skill, request) else "t109",
        )
    else:
        resume_v2 = None
    workflow = (ResumeWorkflow(resume_v2, materials, request.template_id)
                if _resume_workflow_enabled(skill, request) else None)
    if workflow is not None:
        workflow.facts.add("request", request.user_prompt, kind="request")
        workflow.facts.save()
    docx = DocxService(
        workspace,
        skill_dir / "scripts",
        catalog=catalog,
        capabilities=capabilities,
    ) if skill.id == "docx_pro" else None
    ppt = PptService(
        workspace,
        skill_dir,
        catalog=catalog,
        capabilities=capabilities,
    ) if skill.id == "ppt-master" else None

    return DomainServices(
        materials=materials,
        resume=resume,
        resume_v2=resume_v2,
        resume_workflow=workflow,
        docx=docx,
        ppt=ppt,
        cancel_callbacks=[
            lambda permanent: (
                material_service.terminate_all()
                if permanent
                else material_service.terminate_active()
            )
        ],
    )


# 领域模式 Prompt 指引（计划 §8：删除 Prompt 中的底层 exec_cmd 说明）。
# 该段在领域模式下注入 system prompt，明确模型只用领域工具。
_DOMAIN_MODE_BANNER = """\
## 领域工具模式（当前任务启用）

本次任务运行在**领域工具模式**：你只能使用上方列出的领域工具
（read_material / create_content_plan / 本功能领域生成工具 /
ask_user_questions / task_failed / finish_task）。

- **不要**调用 read/write/edit/exec_cmd/spec_append/ingest 等底层工具
  （当前 schema 中没有它们，调用会被拒绝）。
- 材料内容通过 `read_material` 读取（source_id 必须从 [MATERIALS] 的
  document.json 原样复制）；ContentPlan 用 `create_content_plan` 写入。
- 最终产物由领域生成工具在 `artifacts/` 生成并登记；交付时 `finish_task`
  只传领域工具返回的产物相对路径。
- 无 Vision（vision: false）时不要读取图片字节，只按 asset 元数据保守决策。
"""
