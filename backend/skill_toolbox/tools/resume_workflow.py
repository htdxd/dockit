"""面向 agent 的简历语义操作；版本、照片路径和模板原型留在后端。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.contracts.resume import (
    ResumeAcceptRequest,
    ResumeContentV2,
    ResumeGenerateV2Request,
    ResumeHeaderV2,
    ResumePreviewRequest,
    ResumeRepairV2Request,
)
from skill_toolbox.contracts.resume_workflow import Content
from skill_toolbox.resume_facts import ResumeFacts
from skill_toolbox.resume_layout.profiles import (
    SUPPORTED_TEMPLATES as SUPPORTED_TEMPLATES,
)
from skill_toolbox.tools.workspace import atomic_write_json


def compact_agent_result(name, result):
    """模型只接收当前语义状态；完整版本记录仍由编辑服务保存。"""
    data = result.data
    if name == "resume_prepare":
        data = {**data, "materials": [{**doc, "blocks": [
            {key: value for key, value in block.items()
             if key in {"id", "type", "text", "level", "page", "caption", "asset_ids", "source_id"}}
            for block in doc["blocks"]]} for doc in data.get("materials", [])]}
    elif data.get("candidate_id"):
        keep = {"candidate_id", "content", "density", "target_pages", "fits_page_target", "page_count",
                "layout", "largest_entries", "page_fit", "docx", "pdf", "remaining_pages",
                "idempotent_replay", "accepted_revision", "visual", "content_review"}
        if name == "resume_accept":
            keep -= {"content", "layout", "largest_entries", "page_fit"}
        data = {key: value for key, value in data.items() if key in keep}

    def omit_empty(value):
        if isinstance(value, dict):
            return {key: omit_empty(item) for key, item in value.items()
                    if item is not None and item != ""}
        if isinstance(value, list):
            return [omit_empty(item) for item in value]
        return value

    return replace(result, data=omit_empty(data))


def _request_id(operation, payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return operation + "-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


class ResumeWorkflow:
    def __init__(self, engine, materials, template_id):
        self.engine = engine
        self.materials = materials
        self.template_id = template_id
        self.material_ids = None
        self.facts = ResumeFacts(engine.workspace)

    def prepare(self, request):
        ids = request.material_ids
        if ids is None:
            ids = list(self.materials.catalog.irs)
        documents = []
        for material_id in ids:
            ir = self.materials.catalog.ir_for(material_id)
            if ir is None:
                raise ToolError("MATERIAL_UNKNOWN_ID", f"未知材料 {material_id}")
            blocks = []
            for index, block in enumerate(ir.blocks, 1):
                source_id = f"m{material_id[:8]}-b{index}"
                self.facts.add(source_id, block.text or "", kind="material", material_id=material_id,
                               block_id=block.id, page=block.page)
                blocks.append({**block.model_dump(), "source_id": source_id})
            documents.append({
                "material_id": material_id, "name": ir.original_name,
                "reading_status": ("needs_review" if ir.warnings else
                                   "transcribed" if any(b.text.strip() for b in ir.blocks) else
                                   "portrait" if any(a.caption == "独立头像" for a in ir.assets) else "needs_review"),
                "reading_hint": ("图片已在准备阶段处理；文字在 blocks，类型见 assets.caption。warnings 非空时需核实；只有独立头像可直接作为照片。"
                                 if ir.source_format == "image" else "正文已在 blocks 中。"),
                "supplementary_views": ["native_text"] if ir.source_format in {"pdf", "docx"} else [],
                "warnings": ir.warnings,
                "blocks": blocks,
                "assets": self.materials.material_summary(material_id).data["assets"],
            })
        self.material_ids = ids
        self.facts.save()
        geometry = self.engine.profile.geometry
        body_height = geometry.page_bottom_pt - geometry.page_top_pt
        continuation_top = geometry.continuation_page_top_pt
        return OperationResult(ok=True, status="validated", data={
            "candidate_state": self.candidate_state(),
            "template_id": self.template_id,
            "person_fields": sorted(set(self.engine.profile.header_labels.values())),
            "materials": documents,
            "material_review": {"total": len(documents),
                                "needs_review": [{"material_id": d["material_id"], "name": d["name"]}
                                                 for d in documents if d["reading_status"] == "needs_review"]},
            "conversation_sources": [{"source_id": key, "kind": value.get("kind")} for key, value in self.facts.sources.items()
                                     if value.get("kind") != "material"],
            "writing_focus": self.facts.writing_focus(),
            "page_budget": {
                "body_height_pt": round(body_height, 1),
                "continuation_body_height_pt": round(geometry.page_bottom_pt - (
                    geometry.page_top_pt if continuation_top is None else continuation_top), 1),
                "line_height_pt": 18,
                "default_body_font_size_pt": self.engine.profile.base_font_pt,
                "max_lines_before_titles_and_gaps": int(body_height / 18),
                "note": "这是模板默认字号下、未扣除标题和间距的容量上限，实际以测量为准。"
                        "经历丰富且未限定一页时可保留内容分为两三页；只有获准摘要时才精简。",
            },
        }, next_action="文字材料正文已在 materials.blocks 中，不必重复 read_material(view='blocks')。图片转录已提供；仅在 warnings 或内容歧义时按 asset_id 补看原图。结合用户已给信息直接生成；真正缺少必要事实时先 ask_user_questions。PDF/DOCX 疑似漏标题或指标上下文才补看 native_text；原件图片优先于重建图片。")

    def _photo(self, asset_id):
        if not asset_id:
            return {"photo_path": "", "hide_photo": True}
        relative = self.materials.catalog.asset_path(asset_id)
        if not relative:
            raise ToolError("ASSET_UNKNOWN", f"未知照片资产 {asset_id}")
        return {"photo_path": relative, "hide_photo": False}

    def record_native_read(self, result):
        """备用视图读到的原文也是事实来源，避免补回的指标被误判为编造。"""
        data = result.data
        for index, row in enumerate(data.get("blocks", data.get("pages", [])), data["offset"] + 1):
            source_id = f"m{data['material_id'][:8]}-native-{index}"
            self.facts.add(source_id, row["text"], kind="material", material_id=data["material_id"],
                           view="native_text", page=row.get("page"), source_locator=row.get("source_locator"))
            row["source_id"] = source_id
        self.facts.save()

    def _plan_sources(self, photo_asset_id=""):
        ids = self.material_ids if self.material_ids is not None else list(self.materials.catalog.irs)
        selected = [b.id for mid in ids for b in self.materials.catalog.ir_for(mid).blocks]
        if photo_asset_id and photo_asset_id not in selected:
            selected.append(photo_asset_id)
        self.materials.create_content_plan(selected_source_ids=selected)

    def generate(self, request):
        allowed = sorted(set(self.engine.profile.header_labels.values()))
        content = request.content
        unknown = set(content.person) - set(allowed)
        if unknown:
            raise ToolError("FIELD_UNSUPPORTED", f"当前模板不支持个人信息字段 {sorted(unknown)}")
        custom = [item.model_dump() for item in content.personal_fields]
        if len({item["key"] for item in custom}) != len(custom):
            raise ToolError("CONTENT_INVALID", "个人字段 key 必须唯一。")
        for item in custom:
            if item["key"] in content.person and content.person[item["key"]] != item["value"]:
                raise ToolError("CONTENT_INVALID", f"字段 {item['key']} 提供了两个不同的值。")
        fields = {**dict.fromkeys(allowed, ""), **content.person}
        visible_custom = {item["key"] for item in custom if item["value"].strip()}
        hidden = [key for key, value in fields.items() if not value.strip() and key not in visible_custom]
        sections = [self.engine.actions.from_section(s) for s in content.sections]
        if request.font_size_pt is not None:
            for section in sections:
                for entry in section["entries"]:
                    if entry["font_size_pt"] is None:
                        entry["font_size_pt"] = request.font_size_pt
        self._check_ids(sections)
        self.facts.validate_refs(sections)
        self._check_typography(sections)
        internal = ResumeContentV2(
            density=request.density,
            template_id=self.template_id, sections=sections,
            header=ResumeHeaderV2(fields=fields, custom_fields=custom, hidden_fields=hidden,
                                 **self._photo(content.photo_asset_id)),
        )
        self._plan_sources(content.photo_asset_id)
        result = self.engine.generate(ResumeGenerateV2Request(
            template_id=self.template_id, content=internal,
            request_id=_request_id("generate", internal.model_dump()),
        ))
        if result.artifact_id and (not result.data.get("idempotent_replay")
                                   or not self._target_path(result.artifact_id).is_file()):
            self._save_target(result.artifact_id, request.target_pages)
        return self._result(result)

    def _check_typography(self, sections):
        from skill_toolbox.resume_layout.typography import factors

        for section in sections:
            for entry in section["entries"]:
                try:
                    factors(entry, section, self.template_id)
                except ValueError as exc:
                    raise ToolError("TYPOGRAPHY_BOUNDS", str(exc), retryable=True,
                                    suggestion="字号还会乘以条目和栏目缩放；提高字号或恢复缩放，不能让最终正文小于8pt。") from None

    @staticmethod
    def _check_ids(sections):
        keys = [s["key"] for s in sections]
        ids = [e["id"] for s in sections for e in s["entries"]]
        if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
            raise ToolError("CONTENT_INVALID", "栏目 key 和条目 id 必须唯一。")

    def candidate_state(self):
        candidates = []
        for path in sorted((self.engine.workspace / "work" / "resume").glob("*/index.json")):
            index = json.loads(path.read_text(encoding="utf-8"))
            if index.get("current"):
                candidates.append(f"{path.parent.name}@{index['current']}")
        return {"candidate_ids": candidates, "next_action": (
            "使用上述真实 candidate_id 调用 resume_preview 获取内容后编辑，不向用户索取内部 ID。" if candidates else
            "尚无候选；修正生成参数后调用 resume_generate。原始文件名不是候选 ID，不向用户索取内部 ID。")}

    def _candidate(self, candidate_id):
        artifact_id, sep, revision = candidate_id.rpartition("@")
        if not sep or not revision.isdigit():
            raise ToolError("CANDIDATE_UNKNOWN", "请原样使用工具返回的 candidate_id。",
                            suggestion=json.dumps(self.candidate_state(), ensure_ascii=False))
        try:
            return artifact_id, int(revision), self.engine.store.load_revision(artifact_id, int(revision))
        except ToolError as exc:
            raise ToolError(exc.code, str(exc), suggestion=json.dumps(self.candidate_state(), ensure_ascii=False)) from None

    def _target_path(self, artifact_id):
        return self.engine.store.artifact_dir(artifact_id) / "workflow.json"

    def _save_target(self, artifact_id, pages):
        atomic_write_json(self._target_path(artifact_id), {"target_pages": pages})

    def _target(self, artifact_id):
        return json.loads(self._target_path(artifact_id).read_text(encoding="utf-8"))["target_pages"]

    def _external(self, content):
        header = content.get("header") or {}
        hidden = set(header.get("hidden_fields") or [])
        custom = [item for item in header.get("custom_fields", []) if item["key"] not in hidden]
        custom_keys = {item["key"] for item in custom}
        photo = next((a.id for ir in self.materials.catalog.irs.values() for a in ir.assets
                      if Path(a.path) == Path(header.get("photo_path") or ".")), "")
        return Content(
            person={key: value for key, value in header.get("fields", {}).items()
                    if key not in hidden and key not in custom_keys},
            personal_fields=custom, photo_asset_id=photo,
            sections=self.engine.actions.to_sections(content["sections"]),
        )

    def edit(self, request):
        artifact_id, revision, record = self._candidate(request.candidate_id)
        if not request.changes and request.density is None:
            if revision != self.engine.store.index(artifact_id)["current"]:
                raise ToolError("STALE_CANDIDATE", "请使用最新 candidate_id 修改页数上限。")
            result = self.engine.preview(ResumePreviewRequest(artifact_id=artifact_id, revision=revision))
            if result.ok:
                self._save_target(artifact_id, request.target_pages)
            return self._result(result)
        photos = {c.asset_id: self._photo(c.asset_id) for c in request.changes if c.op == "replace_photo"}
        working, actions, header = self.engine.actions.compile(record["content"], request.changes, photo_changes=photos)
        self._external(working)  # 在渲染前拒绝空条目/空简历，不能生成后才发现遗漏。
        self._check_ids(working["sections"])
        self.facts.validate_refs(working["sections"])
        self._check_typography(working["sections"])
        result = self.engine.repair(ResumeRepairV2Request(
            density=request.density,
            artifact_id=artifact_id, base_revision=revision, changes=actions,
            header=ResumeHeaderV2(**header) if header is not None else None,
            request_id=_request_id("edit", request.model_dump()),
        ))
        if request.target_pages is not None and result.ok and not result.data.get("idempotent_replay"):
            self._save_target(artifact_id, request.target_pages)
        current = self.engine.store.index(artifact_id)["current"]
        latest = self.engine.store.load_revision(artifact_id, current)
        self._plan_sources(self._external(latest["content"]).photo_asset_id)
        return self._result(result, changes=[c.model_dump(exclude_unset=True) for c in request.changes])

    def preview(self, request):
        artifact_id, revision, _ = self._candidate(request.candidate_id)
        return self._result(self.engine.preview(ResumePreviewRequest(
            artifact_id=artifact_id, revision=revision, pages=request.pages or [],
        )))

    def accept(self, request):
        artifact_id, revision, record = self._candidate(request.candidate_id)
        if record["page_count"] > self._target(artifact_id):
            raise ToolError("PAGE_TARGET_EXCEEDED", "候选超出页数上限；请编辑内容、字号或提高 target_pages。")
        result = self.engine.accept(ResumeAcceptRequest(
            artifact_id=artifact_id, candidate_revision=revision,
            request_id=_request_id("accept", request.model_dump()),
        ), visual_notes=request.visual_notes)
        return self._result(result)

    def verify_delivery(self, paths: list[str]) -> None:
        """交付只允许当前已接受版本的 DOCX/PDF，不以看过预览代替接受。"""
        if not paths:
            raise ValueError("简历交付文件不能为空。")
        workspace = self.engine.workspace.resolve()
        resolved = [(workspace / path).resolve() for path in paths]
        try:
            relative = resolved[0].relative_to(workspace / "work" / "resume")
            artifact_id, revisions, revision = relative.parts[:3]
            if revisions != "revisions":
                raise ValueError
            revision = int(revision)
        except (ValueError, TypeError):
            raise ValueError("请交付 resume_accept 返回的 DOCX/PDF 路径。") from None
        try:
            index = self.engine.store.index(artifact_id)
            if index["current"] != revision or index.get("accepted") != revision:
                raise ValueError("交付前必须对最新候选调用 resume_accept；旧版或未接受候选不可交付。")
            record = self.engine.store.load_revision(artifact_id, revision)
            allowed = {(workspace / record[key]).resolve() for key in ("docx", "pdf") if record.get(key)}
            if not all(path in allowed for path in resolved):
                raise ValueError("交付文件必须来自同一已接受版本的 DOCX/PDF。")
            from skill_toolbox.tools.workspace import sha256_file
            for kind in ('docx', 'pdf'):
                path = (workspace / record[kind]).resolve() if record.get(kind) else None
                if path in resolved and (not path.is_file() or sha256_file(path) != record.get(kind + '_sha256')):
                    raise ValueError('交付文件与已接受版本的 hash 不一致，必须重新生成或修复。')
            report_path = self.engine.store.revision_dir(artifact_id, revision) / "delivery_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (record.get("mechanical", {}).get("passed") is not True
                    or report.get("visual", {}).get("status") != "passed"):
                raise ValueError("当前候选尚未通过机械检查与视觉接受。")
            if record["page_count"] > self._target(artifact_id):
                raise ValueError("当前候选超出允许页数；请编辑后重新接受。")
        except (ToolError, OSError) as exc:
            raise ValueError(f"简历交付状态不可用：{exc}") from exc

    def _result(self, result, *, changes=None):
        if not result.artifact_id or result.revision is None:
            return result
        record = self.engine.store.load_revision(result.artifact_id, result.revision)
        review = self.facts.review(record["content"])
        atomic_write_json(self.engine.store.artifact_dir(result.artifact_id) / "revisions" /
                          str(result.revision) / "content_review.json", review)
        result.data["content_review"] = {key: value for key, value in review.items() if key != "entries"}
        target = self._target(result.artifact_id)
        fits = record["page_count"] <= target
        rows = [{"target_id": e["instance_id"], "section_id": s["section_key"],
                 "page": e["page_index"] + 1, "lines": e["lines"],
                 "height_pt": e["text_height_pt"]}
                for s in record.get("instance_model", []) for e in s["entries"]]
        result.data.update({
            "candidate_id": f"{result.artifact_id}@{result.revision}",
            "density": record["content"].get("density", "normal"),
            "content": self._external(record["content"]).model_dump(),
            "target_pages": target, "fits_page_target": fits,
            "page_count": record["page_count"], "layout": rows,
            "largest_entries": sorted(rows, key=lambda r: r["height_pt"], reverse=True)[:3],
            "docx": record["docx"], "pdf": record["pdf"],
            "page_fit": {
                **(record.get("single_page_fit") or {}),
                "entries_beyond_target": [row for row in rows if row["page"] > target],
                "first_page_unused_pt": round(max(0, self.engine.profile.geometry.page_bottom_pt - max(
                    (e["region"]["y_pt"] + e["region"]["h_pt"]
                     for s in record.get("instance_model", []) for e in s["entries"]
                     if e["page_index"] == 0), default=self.engine.profile.geometry.page_top_pt)), 1),
            },
        })
        if changes is not None:
            result.data["applied_changes"] = changes
        if not result.ok:
            result.next_action = "先按 issues 修复内容或机械检查问题，再处理页数；不要删除未授权内容来绕过检查。使用当前 candidate_id 局部修复。"
        elif not fits:
            result.ok = False
            result.status = "failed"
            result.issues.append({"code": "PAGE_TARGET_EXCEEDED", "message": f"实际 {record['page_count']} 页，目标最多 {target} 页。"})
            result.next_action = ("先用 resume_edit(candidate_id=当前候选, density='compact') 压缩纵向留白，不要重新生成或用等比缩放压页。"
                                  if record["content"].get("density", "normal") == "normal" else
                                  "已使用紧凑排版，根据 page_fit 的超页条目和首页余量一次批量精简表达；若必须保留全部文字则说明页数限制，不能原样重试。")
            result.next_action += " 不得擅自提高用户页数或删除未授权内容。"
            fit = record.get("single_page_fit") or {}
            if target == 1 and fit.get("required_reduction_pt"):
                result.next_action = (
                    f"当前整页还需节省约 {fit['required_reduction_pt']}pt"
                    f"（约 {fit['approx_lines_to_save']} 行正文高度）。"
                    "请一次批量调整到这个量级；只减少字数但未减少实际换行，不会释放高度。 "
                    + result.next_action)
        else:
            result.next_action = "自行完成全部页面视觉检查；需修改时用 resume_edit，合格后调用 resume_accept，无需询问用户是否满意，再使用接受结果的 DOCX/PDF 路径 finish_task。"
        return result
