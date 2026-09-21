"""简历任务装配：材料准备、工具绑定、问答与交付，统一复用领域工作流。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

from skill_toolbox.agent_loop import ToolDefinition
from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.resume_workflow import PrepareRequest
from skill_toolbox.llm_tools.common import shared_tools
from skill_toolbox.llm_tools.resume_workflow import REQUEST_MODELS, workflow_tools
from skill_toolbox.material_intake import initial_material_context, prepare_images
from skill_toolbox.material_models import QAReport
from skill_toolbox.materials import MaterialService, safe_stem
from skill_toolbox.models import ConversationMessage, ImageContent, ToolResult
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.resume_facts import WRITING_STYLES
from skill_toolbox.runtime_types import TaskResult
from skill_toolbox.tools.materials import MaterialPlanService
from skill_toolbox.tools.resume_edit import ResumeEditService
from skill_toolbox.tools.resume_workflow import ResumeWorkflow, compact_agent_result


def build_workflow(workspace, skill, request, material_service):
    materials = MaterialPlanService(
        workspace, material_service.catalog(), "resume", request.capabilities
    )
    engine = ResumeEditService(
        workspace,
        skill.dir / "templates",
        request.capabilities,
        template_id=request.template_id or "t109",
    )
    flow = ResumeWorkflow(engine, materials, request.template_id or "t109")
    flow.facts.add("request", request.user_prompt, kind="request")
    flow.facts.save()
    return flow


def operation_result(name, result):
    if name.startswith("resume_"):
        result = compact_agent_result(name, result)
    ok = result.ok and result.status != "failed"
    payload = result.as_dict()
    code = None
    if not ok:
        issue = result.issues[0] if result.issues else {}
        code = issue.get("code", "TOOL_FAILED")
        payload.update(
            code=code,
            message=issue.get("message", "操作失败"),
            suggestion=issue.get("suggestion") or result.next_action,
            retryable=bool(issue.get("retryable", False)),
        )
    return ToolResult(
        tool_call_id="",
        name=name,
        success=ok,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        images=[ImageContent.model_validate(i) for i in result.images],
        metadata={
            "error_code": code,
            "message": payload.get("message", ""),
            "revision": result.revision,
            "preview_refs": list(result.preview_refs),
            **{
                key: result.data.get(key)
                for key in ("candidate_id", "page_count", "fits_page_target")
            },
        },
    )


class ResumeTask:
    def __init__(
        self,
        workspace,
        skill,
        request,
        provider,
        emit,
        debug,
        broker,
        model_timeout,
        tool_timeout,
    ):
        self.workspace, self.skill, self.request = workspace, skill, request
        self.provider, self.emit, self.debug, self.broker = (
            provider,
            emit,
            debug,
            broker,
        )
        self.model_timeout, self.tool_timeout = model_timeout, tool_timeout
        self.material_service = MaterialService(
            workspace, request.env, mineru_token=request.mineru_token
        )
        self.workflow = None
        self.measurement_failures = 0

    async def prepare(self):
        request = self.request
        for index, source in enumerate(request.materials):
            if not source.is_file():
                continue
            relative = (
                f"sources/{index}-{safe_stem(source.name) or 'material'}/{source.name}"
            )
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self.debug(
                {
                    "phase": "materials_staged",
                    "workspace": str(self.workspace),
                    "staged": [{"relative": relative, "absolute": str(source)}],
                }
            )
            self.emit(
                {"type": "material_progress", "path": relative, "phase": "parsing"}
            )
            try:
                await asyncio.to_thread(self.material_service.prepare_material, source)
            except Exception as exc:
                self.emit(
                    {
                        "type": "material_progress",
                        "path": relative,
                        "phase": "failed",
                        "error": getattr(exc, "code", "MATERIAL_FAILED"),
                    }
                )
                raise
            finally:
                self.material_service.terminate_active()
            self.emit({"type": "material_progress", "path": relative, "phase": "done"})
        await prepare_images(
            self.material_service,
            self.provider,
            vision=request.capabilities.get("vision", False),
            emit=self.emit,
            debug=self.debug,
            timeout=self.model_timeout,
        )
        self.workflow = build_workflow(
            self.workspace, self.skill, request, self.material_service
        )
        prepared = await asyncio.to_thread(self.workflow.prepare, PrepareRequest())
        user_text = request.user_prompt + initial_material_context(
            compact_agent_result("resume_prepare", prepared).data
        )
        system = self.skill.system_prompt
        system += "\n\n本次写作档位：" + WRITING_STYLES[request.writing_style]
        if request.capabilities:
            system = (
                "[CAPABILITIES]\n"
                + "\n".join(
                    f"{k}: {str(v).lower()}"
                    for k, v in sorted(request.capabilities.items())
                )
                + "\n\n"
                + system
            )
        self.debug(
            {
                "phase": "materials_context_ready",
                "material_count": len(self.workflow.materials.catalog.irs),
                "context_characters": len(user_text),
            }
        )
        return system, [ConversationMessage(role="user", text=user_text)], self.tools()

    def tools(self):
        definitions = {}
        for spec in workflow_tools():
            name = spec["name"]
            model, method = (
                REQUEST_MODELS[name],
                getattr(self.workflow, name.removeprefix("resume_")),
            )

            async def execute(args, name=name, model=model, method=method):
                result = operation_result(
                    name, await asyncio.to_thread(method, model.model_validate(args))
                )
                if result.metadata.get("revision") is not None or result.metadata.get(
                    "preview_refs"
                ):
                    self.emit(
                        {
                            "type": "resume_preview",
                            "tool": name,
                            "revision": result.metadata["revision"],
                            "preview_refs": result.metadata["preview_refs"],
                            "image_count": len(result.images),
                        }
                    )
                return result

            definitions[name] = ToolDefinition(
                spec,
                execute,
                max(
                    self.tool_timeout,
                    420 if name in {"resume_generate", "resume_edit"} else 60,
                ),
            )
        handlers = {
            "read_material": self.read_material,
            "ask_user_questions": self.ask,
            "finish_task": self.finish,
            "task_failed": self.fail,
        }
        for spec in shared_tools():
            name = spec["name"]
            if name in handlers:
                definitions[name] = ToolDefinition(
                    spec,
                    handlers[name],
                    None if name == "ask_user_questions" else self.tool_timeout,
                )
        return definitions

    async def read_material(self, args):
        result = await asyncio.to_thread(
            self.workflow.materials.read_material,
            str(args["source_id"]),
            view=args.get("view", "summary"),
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 200)),
        )
        if result.data.get("view") == "native_text":
            self.workflow.record_native_read(result)
        return operation_result("read_material", result)

    async def ask(self, args):
        questions = args["questions"]
        if re.search(
            r"candidate[_\s]*id|候选\s*ID",
            json.dumps(questions, ensure_ascii=False),
            re.I,
        ):
            raise ToolError(
                "INTERNAL_STATE_QUESTION",
                "候选 ID 是内部状态，不向用户索取。",
                suggestion=json.dumps(
                    self.workflow.candidate_state(), ensure_ascii=False
                ),
            )
        if self.broker is None:
            raise ToolError("USER_INPUT_UNAVAILABLE", "User input is unavailable")
        self.emit({"type": "questions_requested", "questions": questions})
        answers = await self.broker.wait()
        source_id = self.workflow.facts.answer(questions, answers)
        self.emit(
            {
                "type": "questions_answered",
                "count": len(questions),
                "answer_ids": [str(q.get("id", "")) for q in questions],
            }
        )
        return ToolResult(
            tool_call_id="",
            name="ask_user_questions",
            success=True,
            content=json.dumps(
                {"answers": answers, "source_id": source_id}, ensure_ascii=False
            ),
        )

    async def fail(self, args):
        return TaskResult(
            "failed",
            error=str(args.get("error", "")).strip() or "Task aborted by the model",
        )

    async def finish(self, args):
        paths = args.get("artifacts")
        if not isinstance(paths, list) or not paths:
            raise ValueError("finish_task requires at least one artifact")
        self.workflow.materials.validate_content_plan()
        self.workflow.verify_delivery(paths)
        suffix = {"DOCX": ".docx", "PDF": ".pdf"}.get(
            (self.request.output_format or "").upper()
        )
        selected = [
            p for p in paths if suffix is None or Path(p).suffix.lower() == suffix
        ]
        if not selected:
            raise ValueError(f"请交付用户选择的 {self.request.output_format} 文件。")
        policy = WorkspacePolicy(self.workspace)
        output = self.request.output_dir
        output.mkdir(parents=True, exist_ok=True)
        published = []
        for relative in selected:
            source = policy.require_file(relative)
            target = output / f"{source.stem}.resume_pro{source.suffix}"
            index = 2
            while target.exists():
                target = output / f"{source.stem}-{index}.resume_pro{source.suffix}"
                index += 1
            shutil.copy2(source, target)
            published.append(target)
        self.emit_qa()
        return TaskResult("completed", artifacts=published)

    def after_tools(self, results):
        for result in results:
            code = result.metadata.get("error_code")
            if code == "WORD_UNAVAILABLE":
                return TaskResult(
                    "failed",
                    error=f"[WORD_UNAVAILABLE] {result.metadata.get('message', 'Microsoft Word 不可用')}",
                )
            if code == "MEASURE_FAILED":
                self.measurement_failures += 1
                if self.measurement_failures >= 3:
                    return TaskResult(
                        "failed",
                        error="[MEASURE_FAILED] Word 测量已失败 3 次，请执行 start-web.bat --check。",
                    )

    def emit_qa(self):
        path = self.workspace / "work/qa/resume.json"
        if path.exists():
            try:
                qa = QAReport.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            payload = {
                **qa.model_dump(),
                "used_assets": len(qa.used_assets),
                "skipped_assets": len(qa.skipped_assets),
            }
            self.emit({"type": "qa_status", "qa": payload})
            self.debug({"phase": "qa_status", "qa": payload})

    def cancel(self, permanent=False):
        if self.workflow is not None:
            runner = self.workflow.engine.runner
            (runner.cancel if permanent else runner.terminate_all)()
        (
            self.material_service.terminate_all
            if permanent
            else self.material_service.terminate_active
        )()
