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
from skill_toolbox.llm_tools.common import error_text, operation_text
from skill_toolbox.tools.docx import DocxService
from skill_toolbox.tools.materials import MaterialPlanService
from skill_toolbox.tools.ppt import PptService
from skill_toolbox.tools.resume import ResumeService

Handler = Callable[[dict[str, Any]], OperationResult]


@dataclass
class DomainServices:
    """领域 Service 容器（Runtime 按任务装配）。"""

    materials: MaterialPlanService | None = None
    resume: ResumeService | None = None
    # 简历 v2（P2-3）：版本化编辑服务；与 legacy resume 并存，按请求形态分流
    resume_v2: Any | None = None
    docx: DocxService | None = None
    ppt: PptService | None = None
    cancel_callbacks: list[Callable[[bool], None]] = field(default_factory=list, repr=False)

    def cancel_all(self, *, permanent: bool = False) -> None:
        """终止领域 Service 的活动子进程。

        timeout 只终止当前调用并允许模型修正后重试；任务整体取消时 permanent
        会让 ProcessRunner 拒绝再启动新进程。
        """
        seen: set[int] = set()
        for service in (self.resume, self.docx, self.ppt):
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

        request = ResumeGenerateRequest(
            template_id=str(args["template_id"]),
            fields={str(k): str(v) for k, v in args.get("fields", {}).items()},
            photo_asset_id=str(args.get("photo_asset_id", "")),
            target_role=str(args.get("target_role", "")),
            formats=[str(f) for f in args.get("formats", ["docx"])],
        )
        return services.resume.generate(request)

    def _resume_repair(args: dict[str, Any]) -> OperationResult:
        _require(services.resume, "ResumeService")
        from skill_toolbox.contracts.resume import ResumeChange, ResumeChangeRequest

        changes = []
        for item in args.get("changes", []):
            changes.append(
                ResumeChange(
                    action=str(item["action"]),
                    component_id=str(item.get("component_id", "")),
                    text=str(item.get("text", "")),
                    move_rows=int(item.get("move_rows", 0)),
                    resize_rows=int(item.get("resize_rows", 0)),
                    asset_id=str(item.get("asset_id", "")),
                )
            )
        return services.resume.repair(
            ResumeChangeRequest(artifact_id=str(args["artifact_id"]), changes=changes)
        )

    handlers["resume_prepare"] = _resume_prepare
    handlers["resume_generate"] = _resume_generate
    handlers["resume_repair"] = _resume_repair

    # ---------- 简历 v2（P2-3：版本化编辑 + 多模态预览） ----------
    def _resume_service_v2() -> Any:
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
        from skill_toolbox.contracts.resume import (
            ResumeContentV2,
            ResumeGenerateV2Request,
        )

        service = _resume_service_v2()
        content_payload = args.get("content") or {}
        mode = args.get("layout_mode") or content_payload.get("layout_mode") or "reflow"
        request = ResumeGenerateV2Request(
            template_id=str(args["template_id"]),
            content=ResumeContentV2(**content_payload),
            request_id=str(args.get("request_id", "")),
            layout_mode=str(mode),  # type: ignore[arg-type]
        )
        return service.generate(request)

    def _resume_repair_v2(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeEditV2, ResumeRepairV2Request

        service = _resume_service_v2()
        changes = [ResumeEditV2(**item) for item in args.get("changes", [])]
        return service.repair(
            ResumeRepairV2Request(
                artifact_id=str(args["artifact_id"]),
                base_revision=int(args["base_revision"]),
                request_id=str(args["request_id"]),
                changes=changes,
                layout_mode=args.get("layout_mode"),
            )
        )

    def _resume_accept(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeAcceptRequest

        service = _resume_service_v2()
        return service.accept(
            ResumeAcceptRequest(
                artifact_id=str(args["artifact_id"]),
                candidate_revision=int(args["candidate_revision"]),
                expected_accepted_revision=int(args["expected_accepted_revision"]),
                request_id=str(args.get("request_id", "")),
            ),
            visual_notes=str(args.get("visual_notes", "")),
        )

    def _resume_restore(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumeRestoreRequest

        service = _resume_service_v2()
        return service.restore(
            ResumeRestoreRequest(
                artifact_id=str(args["artifact_id"]),
                target_revision=int(args["target_revision"]),
                expected_accepted_revision=int(args["expected_accepted_revision"]),
                request_id=str(args.get("request_id", "")),
            )
        )

    def _resume_preview(args: dict[str, Any]) -> OperationResult:
        from skill_toolbox.contracts.resume import ResumePreviewRequest

        service = _resume_service_v2()
        return service.preview(
            ResumePreviewRequest(
                artifact_id=str(args["artifact_id"]),
                revision=int(args["revision"]),
                pages=[int(p) for p in args.get("pages", [])],
            )
        )

    handlers["resume_prepare_v2"] = _resume_prepare_v2
    handlers["resume_generate_v2"] = _resume_generate_v2
    handlers["resume_repair_v2"] = _resume_repair_v2
    handlers["resume_accept"] = _resume_accept
    handlers["resume_restore"] = _resume_restore
    handlers["resume_preview"] = _resume_preview

    # ---------- DOCX（阶段 5） ----------
    def _docx_start(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxStartRequest

        return services.docx.start(
            DocxStartRequest(
                title=str(args["title"]),
                complexity=str(args.get("complexity", "standard")),  # type: ignore[arg-type]
                scene=str(args.get("scene", "")),
                author=str(args.get("author", "")),
                date=str(args.get("date", "")),
            )
        )

    def _docx_add_blocks(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxAddBlocksRequest, DocxBlock

        blocks = []
        for item in args.get("blocks", []):
            blocks.append(
                DocxBlock(
                    type=str(item["type"]),  # type: ignore[arg-type]
                    text=str(item.get("text", "")),
                    level=int(item.get("level", 1)),
                    asset_id=str(item.get("asset_id", "")),
                    caption=str(item.get("caption", "")),
                    headers=[str(h) for h in item.get("headers", [])],
                    rows=[[str(c) for c in row] for row in item.get("rows", [])],
                    latex=str(item.get("latex", "")),
                )
            )
        return services.docx.add_blocks(
            DocxAddBlocksRequest(document_id=str(args["document_id"]), blocks=blocks)
        )

    def _docx_finalize(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxFinalizeRequest

        return services.docx.finalize(
            DocxFinalizeRequest(
                document_id=str(args["document_id"]),
                output_name=str(args.get("output_name", "")),
            )
        )

    def _docx_repair(args: dict[str, Any]) -> OperationResult:
        _require(services.docx, "DocxService")
        from skill_toolbox.contracts.docx import DocxRepairRequest

        return services.docx.repair(
            DocxRepairRequest(
                artifact_id=str(args["artifact_id"]),
                issue_id=str(args["issue_id"]),
                fix=args.get("fix", {}),
            )
        )

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
        from skill_toolbox.contracts.ppt import PptGenerateRequest, SlideSpec

        slides = []
        for item in args.get("slides", []):
            slides.append(
                SlideSpec(
                    layout=str(item.get("layout", "bullets")),  # type: ignore[arg-type]
                    title=str(item.get("title", "")),
                    bullets=[str(b) for b in item.get("bullets", [])],
                    body=str(item.get("body", "")),
                    asset_id=str(item.get("asset_id", "")),
                    columns=[str(c) for c in item.get("columns", [])],
                    table_headers=[str(h) for h in item.get("table_headers", [])],
                    table_rows=[[str(c) for c in row] for row in item.get("table_rows", [])],
                    notes=str(item.get("notes", "")),
                )
            )
        return services.ppt.generate(
            PptGenerateRequest(
                outline_id=str(args["outline_id"]),
                slides=slides,
                template_id=str(args.get("template_id", "")),
            )
        )

    def _ppt_repair(args: dict[str, Any]) -> OperationResult:
        _require(services.ppt, "PptService")
        from skill_toolbox.contracts.ppt import PptRepairRequest

        return services.ppt.repair(
            PptRepairRequest(
                artifact_id=str(args["artifact_id"]),
                issues=[str(i) for i in args.get("issues", [])],
                changes=[dict(c) for c in args.get("changes", [])],
            )
        )

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
        "ok": result.ok,
    }
    if result.ok:
        return operation_text(result), list(result.images), meta
    issue = result.issues[0] if result.issues else {}
    suggestion = issue.get("suggestion")
    return (
        error_text(
            ToolError(
                str(issue.get("code", "TOOL_FAILED")),
                str(issue.get("message", "操作失败")),
                retryable=bool(issue.get("retryable", False)),
                suggestion=str(suggestion) if suggestion is not None else None,
            )
        ),
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
