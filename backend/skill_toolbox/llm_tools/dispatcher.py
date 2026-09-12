"""LLM tool call → 内部 Service 的 dispatcher（实施计划 §3.1/§8）。

领域工具由 Runtime 按 Skill 组装（shared + 领域 schema）；本模块把 LLM
tool call 翻译为内部 typed Service 调用，返回 LLM 可见的固定文本格式。
参数解析失败/Service 失败都转稳定 ToolError 文本（含 code/retryable/
suggestion），不返回 traceback。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from skill_toolbox.contracts.common import OperationResult, ToolError
import json

from skill_toolbox.llm_tools.common import error_text, operation_text
from skill_toolbox.tools.docx import DocxService
from skill_toolbox.tools.materials import MaterialPlanService
from skill_toolbox.tools.ppt import PptService
from skill_toolbox.tools.resume import ResumeEditService, ResumeService
from skill_toolbox.tools.resume_workflow import ResumeWorkflow

Handler = Callable[[dict[str, Any]], OperationResult]


@dataclass
class DomainServices:
    """领域 Service 容器（Runtime 按任务装配）。"""

    materials: MaterialPlanService | None = None
    resume: ResumeService | None = None
    # 简历 v2（P2-3）：版本化编辑服务；与 legacy resume 并存，按请求形态分流
    resume_v2: ResumeEditService | None = None
    resume_workflow: ResumeWorkflow | None = None
    docx: DocxService | None = None
    ppt: PptService | None = None
    cancel_callbacks: list[Callable[[bool], None]] = field(default_factory=list, repr=False)

    def cancel_all(self, *, permanent: bool = False) -> None:
        """终止领域 Service 的活动子进程。

        timeout 只终止当前调用并允许模型修正后重试；任务整体取消时 permanent
        会让 ProcessRunner 拒绝再启动新进程。
        """
        seen: set[int] = set()
        for service in (self.resume, self.resume_v2, self.docx, self.ppt):
            runner = getattr(service, "runner", None)
            if runner is None or id(runner) in seen:
                continue
            seen.add(id(runner))
            method = getattr(runner, "cancel" if permanent else "terminate_all", None)
            if callable(method):
                method()
        for callback in self.cancel_callbacks:
            callback(permanent)


def build_dispatcher(services: DomainServices) -> dict[str, Handler]:
    """注册 3 个领域的领域工具处理器。返回 {tool_name: handler}。"""
    handlers: dict[str, Handler] = {}

    # ---------- 共享材料（阶段 3） ----------
    def _read_material(args: dict[str, Any]) -> OperationResult:
        _require(services.materials, "MaterialPlanService")
        return services.materials.read_material(
            str(args["source_id"]),
            view=str(args.get("view", "summary")),  # type: ignore[arg-type]
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 200)),
        )

    def _create_content_plan(args: dict[str, Any]) -> OperationResult:
        _require(services.materials, "MaterialPlanService")
        return services.materials.create_content_plan(
            [str(v) for v in args.get("selected_source_ids", [])],
            [str(v) for v in args.get("excluded_source_ids", [])],
            {str(k): str(v) for k, v in args.get("exclusion_reasons", {}).items()},
        )

    handlers["read_material"] = _read_material
    handlers["create_content_plan"] = _create_content_plan

    # ---------- 简历（阶段 4） ----------
    def _resume_prepare(args: dict[str, Any]) -> OperationResult:
        _require(services.resume, "ResumeService")
        return services.resume.prepare(str(args["template_id"]))

    def _resume_generate(args: dict[str, Any]) -> OperationResult:
        _require(services.resume, "ResumeService")
        from skill_toolbox.contracts.resume import ResumeGenerateRequest

        request = ResumeGenerateRequest.model_validate(args)
        return services.resume.generate(request)

    def _resume_repair(args: dict[str, Any]) -> OperationResult:
        _require(services.resume, "ResumeService")
        from skill_toolbox.contracts.resume import ResumeChangeRequest

        return services.resume.repair(ResumeChangeRequest.model_validate(args))

    handlers["resume_prepare"] = _resume_prepare
    handlers["resume_generate"] = _resume_generate
    handlers["resume_repair"] = _resume_repair

    # ---------- 简历 v2（P2-3：版本化编辑 + 多模态预览） ----------
    def _resume_service_v2() -> ResumeEditService:
        service = services.resume_v2
        _require(service, "ResumeEditService")
        return service

    def _resume_prepare_v2(args: dict[str, Any]) -> OperationResult:
        return _resume_service_v2().prepare(
            str(args["template_id"]),
            artifact_id=str(args.get("artifact_id", "")),
            revision=(int(args["revision"]) if args.get("revision") is not None else None),
        )

    def _resume_generate_v2(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeGenerateV2Request

        service = _resume_service_v2()
        content_payload = args.get("content") or {}
        mode = args.get("layout_mode") or content_payload.get("layout_mode") or "reflow"
        request = ResumeGenerateV2Request.model_validate({**args, "layout_mode": mode})
        return service.generate(request)

    def _resume_repair_v2(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeRepairV2Request

        service = _resume_service_v2()
        return service.repair(ResumeRepairV2Request.model_validate(args))

    def _resume_accept(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeAcceptRequest

        service = _resume_service_v2()
        return service.accept(
            ResumeAcceptRequest.model_validate(args),
            visual_notes=str(args.get("visual_notes", "")),
        )

    def _resume_restore(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeRestoreRequest

        service = _resume_service_v2()
        return service.restore(ResumeRestoreRequest.model_validate(args))

    def _resume_preview(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumePreviewRequest

        service = _resume_service_v2()
        return service.preview(ResumePreviewRequest.model_validate(args))

    handlers["resume_prepare_v2"] = _resume_prepare_v2
    handlers["resume_generate_v2"] = _resume_generate_v2
    handlers["resume_repair_v2"] = _resume_repair_v2
    handlers["resume_accept"] = _resume_accept
    handlers["resume_restore"] = _resume_restore
    handlers["resume_preview"] = _resume_preview

    if services.resume_workflow is not None:
        from skill_toolbox.llm_tools.resume_workflow import REQUEST_MODELS

        for name in tuple(handlers):
            if name.startswith("resume_") and name not in REQUEST_MODELS:
                del handlers[name]
        for name, method in {
            "resume_prepare": "prepare", "resume_generate": "generate", "resume_edit": "edit",
            "resume_preview": "preview", "resume_accept": "accept",
        }.items():
            model = REQUEST_MODELS[name]
            operation = getattr(services.resume_workflow, method)
            handlers[name] = lambda args, model=model, operation=operation: operation(model.model_validate(args))

    # ---------- DOCX（阶段 5） ----------
    def _docx_start(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxStartRequest

        return services.docx.start(DocxStartRequest.model_validate(args))

    def _docx_add_blocks(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxAddBlocksRequest

        return services.docx.add_blocks(DocxAddBlocksRequest.model_validate(args))

    def _docx_finalize(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxFinalizeRequest

        return services.docx.finalize(DocxFinalizeRequest.model_validate(args))

    def _docx_repair(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxRepairRequest

        return services.docx.repair(DocxRepairRequest.model_validate(args))

    handlers["docx_start"] = _docx_start
    handlers["docx_add_blocks"] = _docx_add_blocks
    handlers["docx_finalize"] = _docx_finalize
    handlers["docx_repair"] = _docx_repair

    # ---------- PPT（阶段 7） ----------
    def _ppt_create_outline(args: dict[str, Any]) -> OperationResult:
        _require(services.ppt, "PptService")
        return services.ppt.create_outline(
            topic=str(args["topic"]),
            audience=str(args.get("audience", "")),
            page_count=int(args.get("page_count", 8)),
            style=str(args.get("style", "clean")),
            selected_source_ids=[str(v) for v in args.get("selected_source_ids", [])],
        )

    def _ppt_generate(args: dict[str, Any]) -> OperationResult:
        _require(services.ppt, "PptService")
        from skill_toolbox.contracts.ppt import PptGenerateRequest

        return services.ppt.generate(PptGenerateRequest.model_validate(args))

    def _ppt_repair(args: dict[str, Any]) -> OperationResult:
        _require(services.ppt, "PptService")
        from skill_toolbox.contracts.ppt import PptRepairRequest

        return services.ppt.repair(PptRepairRequest.model_validate(args))

    handlers["ppt_create_outline"] = _ppt_create_outline
    handlers["ppt_generate"] = _ppt_generate
    handlers["ppt_repair"] = _ppt_repair

    return handlers


def dispatch_with_media(
    name: str,
    arguments: dict[str, Any],
    services: DomainServices,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """执行领域工具，返回 (LLM 可见文本, 内联图像, 元数据)。

    图像来自 ``OperationResult.images``（P2-3：自动渲染后的预览图必须真正进入
    模型视觉输入，而不是只在文本里给路径）。元数据含 revision / preview_refs，
    由 Runtime 记录日志与事件。
    """
    if "_invalid_json" in arguments:
        truncated = arguments.get("_finish_reason") == "length"
        return error_text(ToolError(
            "MODEL_OUTPUT_TRUNCATED" if truncated else "TOOL_JSON_INVALID",
            "模型工具参数因输出长度限制被截断。" if truncated else "模型返回了不完整或非法的 JSON 工具参数。",
            retryable=True,
            suggestion="完整重发这次工具调用；保持 JSON 完整，必要时减少单次内容或拆分编辑操作。",
        )), [], {}
    handlers = build_dispatcher(services)
    handler = handlers.get(name)
    if handler is None:
        return (
            error_text(
                ToolError(
                    "TOOL_NOT_AVAILABLE",
                    f"未知领域工具: {name}。可用: {sorted(handlers)}",
                )
            ),
            [],
            {},
        )
    try:
        result = handler(arguments)
    except ToolError as exc:
        return error_text(exc), [], {}
    except (KeyError, TypeError, ValueError) as exc:
        return (
            error_text(
                ToolError(
                    "TOOL_ARGUMENTS_INVALID",
                    f"工具参数解析失败: {exc}",
                    retryable=True,
                    suggestion="检查参数名与类型是否符合工具 schema。",
                )
            ),
            [],
            {},
        )
    meta = {
        "revision": result.revision,
        "preview_refs": list(result.preview_refs),
        "ok": result.ok and result.status != "failed",
    }
    if result.ok:
        return operation_text(result), list(result.images), meta
    issue = result.issues[0] if result.issues else {}
    suggestion = issue.get("suggestion")
    error = ToolError(
        str(issue.get("code", "TOOL_FAILED")), str(issue.get("message", "操作失败")),
        retryable=bool(issue.get("retryable", False)),
        suggestion=str(suggestion) if suggestion is not None else result.next_action,
    ).to_dict()
    if result.artifact_id:
        error.update(artifact_id=result.artifact_id, revision=result.revision,
                     data=result.data, issues=result.issues, next_action=result.next_action)
    return (
        json.dumps(error, ensure_ascii=False),
        list(result.images),
        meta,
    )


def dispatch(
    name: str,
    arguments: dict[str, Any],
    services: DomainServices,
) -> str:
    """执行领域工具并返回 LLM 可见文本（兼容入口；图像由 dispatch_with_media 传递）。"""
    text, _images, _meta = dispatch_with_media(name, arguments, services)
    return text


def _require(service: object | None, name: str) -> None:
    if service is None:
        raise ToolError(
            "SERVICE_NOT_CONFIGURED",
            f"{name} 未配置（当前任务未启用该领域）。",
        )
