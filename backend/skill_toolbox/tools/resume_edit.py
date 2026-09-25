"""简历 v2 编辑服务：编排版本、布局、渲染与候选验收。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.resume_actions import (
    MAX_BODY_H_PT,
    MIN_BODY_W_PT,
    V2_MIN_ENTRY_GAP_PT,
    PreparedEdit,
    ResumeActions,
)
from skill_toolbox.resume_content import (
    canonical_entry,
    detail_groups,
    is_project,
    title_metrics,
)
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.tools.resume_store import ResumeStore
from skill_toolbox.tools.workspace import atomic_write_json, sha256_file


def _normalize_photo(path: Path) -> bytes:
    """把常见图片统一成 PNG；各模板写入对应部件并声明正确的媒体类型。"""
    import io

    from PIL import Image

    try:
        with Image.open(path) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG")
    except OSError as exc:
        raise ToolError(
            "ASSET_UNKNOWN", f"照片无法解码: {path.name}（{exc}）"
        ) from None
    return buf.getvalue()


# 图像预算（初始值；真实 provider 探针后固化 —— 见 SCHEMA_FREEZE §5）
IMAGE_BUDGET_PAGES = 3
IMAGE_MAX_BYTES = 2 * 1024 * 1024
IMAGE_TOTAL_BYTES = 4 * 1024 * 1024


def _payload_hash(payload: object) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _pdf_creator(pdf: Path) -> str:
    """记录实际导出应用（Word/WPS/LibreOffice 写入 PDF creator），不按配置猜测。"""
    if not pdf.is_file():
        return "unknown"
    import pymupdf

    with pymupdf.open(pdf) as document:
        return document.metadata.get("creator") or document.metadata.get("producer") or "unknown"


class ResumeEditService:
    """候选版本、幂等提交、验收与预览；动作和构建委托给共享模块。"""

    def __init__(
        self,
        workspace: Path,
        templates_root: Path,
        capabilities: dict[str, bool] | None = None,
        runner: ProcessRunner | None = None,
        template_id: str = "t109",
    ) -> None:
        # Word COM 只认绝对路径（它按自身进程 cwd 解析相对路径），因此 workspace
        # 一律解析为绝对路径；所有产物路径都从这里派生。
        from skill_toolbox.resume_layout.profiles import get_profile
        self.profile = get_profile(template_id)
        self.actions = ResumeActions(self.profile)
        self.template_id = template_id
        self.geometry = self.profile.geometry
        self.archive_rel = f"work/resume/{template_id}_spacing_archive.json"
        self.workspace = Path(workspace).resolve()
        self.templates_root = Path(templates_root).resolve()
        self.runner = runner or ProcessRunner()
        self.store = ResumeStore(self.workspace)
        self.capabilities = capabilities or {}
        self.vision = bool(self.capabilities.get("vision", False))

    # ---------- 路径与版本存储 ----------

    def _request_signature(self, op: str, params: dict[str, Any]) -> str:
        """请求级幂等签名（含 op）：与当前版本号无关，重试时稳定。"""
        return _payload_hash({"op": op, "params": params})

    def _replay_request(self, entry: dict[str, Any]) -> OperationResult | None:
        """命中幂等记录 → 返回**原样**结果（不重新执行任何写操作）。"""
        kind = entry.get("kind")
        if kind == "revision":
            record = self.store.load_revision(
                str(entry["artifact_id"]), int(entry["revision"])
            )
            return self._result_from_record(
                str(entry["artifact_id"]), record, idempotent=True
            )
        if kind == "accept":
            data = dict(entry.get("data") or {})
            return OperationResult(
                ok=True,
                status="validated",
                artifact_id=str(entry["artifact_id"]),
                revision=int(entry["revision"]),
                data=data,
                next_action="交付或继续 resume_repair_v2 调整。",
            )
        return None

    def _replay_or_none(
        self, request_id: str, payload_sig: str, *, reserve: bool = False
    ) -> OperationResult | None:
        """锁内调用时先绑定请求载荷，再执行生成；失败后同载荷仍可重试。"""
        if reserve and request_id:
            with self.store.request_lock():
                prev = self.store.lookup_request(request_id, payload_sig)
                if prev is None:
                    index = self.store.request_index()
                    index[request_id] = {"payload": payload_sig, "kind": "pending"}
                    self.store.save_request_index(index)
        else:
            prev = self.store.lookup_request(request_id, payload_sig)
        if prev is None:
            return None
        return self._replay_request(prev)

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace.resolve()).as_posix()

    # ---------- 模板与间距档案 ----------

    def _template_dir(self, template_id: str) -> Path:
        tpl = (self.templates_root / template_id).resolve()
        if template_id != self.template_id or not (tpl / "template.docx").is_file():
            raise ToolError(
                "TEMPLATE_UNKNOWN",
                f"本任务绑定组件模板 {self.template_id}（收到 {template_id}）。"
                "请在新任务中选择已组件化的模板。",
            )
        manifest = json.loads((tpl / 'manifest.json').read_text(encoding='utf-8'))
        if sha256_file(tpl / 'template.docx') != manifest['template_sha256']:
            raise ToolError('TEMPLATE_CHANGED', '模板原件已变化，不能继续使用旧组件定义；请恢复模板或重新组件化。')
        return tpl


    def _archive(self):
        from skill_toolbox.resume_layout.component_template import ensure_archive
        return ensure_archive(self._template_dir(self.template_id) / 'template.docx',
                              self.workspace / self.archive_rel, self.template_id)


    def _run_stage(self, command: list[str], error_code: str) -> None:
        try:
            result = self.runner.run(command, cwd=self.workspace, timeout=300,
                                     extra_env={"PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired as exc:
            raise ToolError(error_code, "文档处理超时。", retryable=True) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
            if 'WORD_UNAVAILABLE:' in detail:
                message = detail.rsplit('WORD_UNAVAILABLE:', 1)[-1].splitlines()[0]
                raise ToolError('WORD_UNAVAILABLE', message, retryable=False)
            raise ToolError(error_code, detail[-2000:], retryable=True)

    # ---------- prepare ----------

    def prepare(
        self, template_id: str, artifact_id: str = "", revision: int | None = None
    ) -> OperationResult:
        tpl_dir = self._template_dir(template_id)
        template = tpl_dir / "template.docx"
        archive = self._archive()
        actions = [
            {"op": "update_entry", "bounds": {"bullets_max": 8, "text_max_chars": 400}},
            {"op": "insert_entry", "bounds": {"entries_max": 20}},
            {"op": "remove_entry"},
            {"op": "move_entry"},
            {"op": "insert_section", "bounds": {"prototypes": ["experience_v1", "plain_lines_v1"]}},
            {"op": "remove_section"},
            {"op": "update_section_title", "bounds": {"title_max_chars": 40}},
            {"op": "set_entry_gap", "bounds": {"gap_pt": [V2_MIN_ENTRY_GAP_PT, 24.0]}},
            {"op": "move_component", "bounds": {"dy_pt": [-120.0, 120.0], "dx_pt": [0.0, 0.0]}},
            {"op": "resize_component", "bounds": {
                "width_pt": [MIN_BODY_W_PT, self.profile.body_width_pt],
                "height_pt": [0.0, MAX_BODY_H_PT],
                "height_delta_pt": [0.0, 200.0],
            }},
            {"op": "format_component", "bounds": {
                "font_size_pt": [8.0, 18.0], "scale": [0.75, 1.25],
            }},
        ]

        data: dict[str, Any] = {
            "template_id": template_id,
            "template_sha256": sha256_file(template)[:16],
            "schema_version": "resume-content-v2",
            "prototypes": ["experience_v1", "plain_lines_v1"],
            "actions": actions,
            "vision": self.vision,
            "layout_modes": {
                "reflow": "目标条目移位/改高后，同页后续条目一起平移（默认）",
                "local": "只动目标条目；与邻居相撞或越界即整批失败",
            },
            "header_fields": sorted(set(self.profile.header_labels.values())),
            "header_photo": {"supported": True, "part": self.profile.photo_part},
            "entry_gap_archive_pt": {
                sid: s.entry_gap_pt for sid, s in archive.sections.items()
            },
        }
        if artifact_id:
            index = self.store.index(artifact_id)
            rev = int(revision if revision is not None else index["current"])
            record = self.store.load_revision(artifact_id, rev)
            data["artifact_id"] = artifact_id
            data["revision"] = rev
            data["accepted_revision"] = index.get("accepted")
            data["page_count"] = record.get("page_count")
            data["sections"] = record.get("instance_model", [])
        return OperationResult(ok=True, status="validated", data=data,
                               revision=data.get("revision"))

    # ---------- generate / repair ----------

    def generate(self, request: Any) -> OperationResult:
        content = request.content.model_dump()
        if content.get("density") == "compact":
            from skill_toolbox.resume_layout.typography import apply_density
            apply_density(content, "compact", self.template_id)
        template_id = request.template_id or content.get("template_id", "")
        self._template_dir(template_id)
        content["template_id"] = template_id
        # header 校验前移：未知键/缺失照片必须在**做任何工作之前**显式拒绝
        self._header_payload(content)
        layout_mode = request.layout_mode or content.get("layout_mode", "reflow")
        request_id = request.request_id or _payload_hash(content)
        payload_sig = self._request_signature(
            "generate", {"content": content, "layout_mode": layout_mode}
        )
        # 请求级幂等先行（快路径）：重试（同 id 同载荷）必须重放原结果
        replayed = self._replay_or_none(request_id, payload_sig)
        if replayed is not None:
            return replayed
        artifact_id = f"resume-{_payload_hash(content)[:8]}"
        with self.store.artifact_lock(artifact_id):
            # 锁内**二次**幂等查询：并发的相同请求可能在我们等待锁期间已完成，
            # 此时必须重放它的结果，而不是继续执行（否则第二次会因版本已推进
            # 而报 VERSION_CONFLICT，重试不再幂等）。
            replayed = self._replay_or_none(request_id, payload_sig, reserve=True)
            if replayed is not None:
                return replayed
            return self._create_revision(
                artifact_id=artifact_id,
                content=content,
                base_revision=None,
                request_id=request_id,
                layout_mode=layout_mode,
                changes=[],
                request_payload=payload_sig,
                request_kind="revision",
            )

    def repair(self, request: Any, *, prepared: PreparedEdit | None = None) -> OperationResult:
        header_change = getattr(request, "header", None)
        density = getattr(request, "density", None)
        if not request.changes and header_change is None and density is None:
            raise ToolError("CONTENT_INVALID", "changes、density 或 header 不能为空")
        header_payload = header_change.model_dump(exclude_unset=True) if header_change is not None else None
        # 幂等签名用**请求本身**（base_revision + changes + layout_mode），与执行
        # 后的版本号无关——否则超时重试会因版本已推进而误报 VERSION_CONFLICT。
        changes_payload = [c.model_dump(exclude_none=True) for c in request.changes]
        payload_sig = self._request_signature(
            "repair",
            {
                "artifact_id": request.artifact_id,
                "base_revision": int(request.base_revision),
                "changes": changes_payload,
                **({"header": header_payload} if header_payload is not None else {}),
                **({"density": density} if density is not None else {}),
                "layout_mode": request.layout_mode,
            },
        )
        replayed = self._replay_or_none(request.request_id, payload_sig)
        if replayed is not None:
            return replayed
        with self.store.artifact_lock(request.artifact_id):
            # 锁内二次查询：并发的相同 repair 请求在我们等锁期间可能已完成，
            # 这时要重放它的结果（而不是撞上 base_revision 已过期）
            replayed = self._replay_or_none(request.request_id, payload_sig, reserve=True)
            if replayed is not None:
                return replayed
            index = self.store.index(request.artifact_id)
            if int(request.base_revision) != int(index["current"]):
                raise ToolError(
                    "VERSION_CONFLICT",
                    f"base_revision={request.base_revision} 不是当前可编辑版本"
                    f"（current={index['current']}，accepted={index.get('accepted')}）。",
                    retryable=True,
                    suggestion=f"用 resume_prepare_v2(artifact_id, revision={index['current']}) "
                               "取当前版本后重试。",
                )
            base = self.store.load_revision(request.artifact_id, int(request.base_revision))
            content = base["content"]
            if prepared is None:
                new_content, records = self.actions.apply(content, request.changes)
            else:
                # prepared 仅由服务端动作模块生成；版本核对通过后直接提交，避免再次应用动作。
                new_content, records = copy.deepcopy(prepared.content), copy.deepcopy(prepared.changes)
            if density is not None:
                from skill_toolbox.resume_layout.typography import apply_density
                apply_density(new_content, density, self.template_id)
                records.append({"op": "set_density", "density": density})
            if header_payload is not None:
                header = dict(new_content.get("header") or {})
                new_content["header"] = header
                patch = dict(header_payload)
                if "fields" in patch:
                    header.setdefault("fields", {}).update(patch.pop("fields"))
                if patch.get("photo_path") and "hide_photo" not in patch:
                    patch["hide_photo"] = False
                header.update(patch)
                records.append({"op": "update_header", "fields": sorted(header_payload)})
            layout_mode = request.layout_mode or base.get("layout_mode", "reflow")
            return self._create_revision(
                artifact_id=request.artifact_id,
                content=new_content,
                base_revision=int(request.base_revision),
                request_id=request.request_id,
                layout_mode=layout_mode,
                changes=records,
                request_payload=payload_sig,
                request_kind="revision",
            )

    # ---------- accept / restore / preview ----------

    def accept(self, request: Any, visual_notes: str = "") -> OperationResult:
        notes = str(visual_notes or "").strip()
        payload_sig = self._request_signature(
            "accept",
            {
                "artifact_id": request.artifact_id,
                "candidate_revision": int(request.candidate_revision),
                "expected_accepted_revision": (
                    int(request.expected_accepted_revision)
                    if request.expected_accepted_revision is not None else None
                ),
                "visual_notes": notes,
            },
        )
        # 幂等先行：成功接受的同一请求重发必须返回原结果，而不是 VERSION_CONFLICT
        replayed = self._replay_or_none(request.request_id, payload_sig)
        if replayed is not None:
            return replayed
        with self.store.artifact_lock(request.artifact_id):
            # 锁内二次查询：并发相同 accept 在等待期间可能已完成
            replayed = self._replay_or_none(request.request_id, payload_sig, reserve=True)
            if replayed is not None:
                return replayed
            index = self.store.index(request.artifact_id)
            rev = int(request.candidate_revision)
            # 过期候选必须拒绝：否则会把旧版本的机械/视觉证据混进当前产物
            if rev != int(index["current"]):
                raise ToolError(
                    "STALE_CANDIDATE",
                    f"candidate_revision={rev} 不是当前版本（current={index['current']}）。"
                    "过期候选不得接受——它的机械与视觉证据属于旧内容。",
                    retryable=True,
                    suggestion=f"对当前版本重新看图后接受 candidate_revision="
                               f"{index['current']}；或先 resume_restore 回到旧版本。",
                )
            if (request.expected_accepted_revision is not None
                    and int(request.expected_accepted_revision) != int(index.get("accepted") or 0)):
                raise ToolError(
                    "VERSION_CONFLICT",
                    f"expected_accepted_revision={request.expected_accepted_revision} "
                    f"≠ 当前 accepted={index.get('accepted')}。",
                    retryable=True,
                )
            record = self.store.load_revision(request.artifact_id, rev)
            mech = record.get("mechanical", {})
            if mech.get("passed") is not True:
                raise ToolError(
                    "MECHANICAL_FAILED",
                    f"revision {rev} 机械门未通过：{mech.get('errors')}",
                )
            visual = record.get("visual", {})
            if self.vision:
                pages = int(record.get("page_count") or 0)
                delivered = sorted(int(p) for p in visual.get("delivered_pages", []))
                if delivered != list(range(1, pages + 1)):
                    raise ToolError(
                        "VISUAL_PENDING",
                        f"revision {rev} 视觉覆盖不完整：已投递页 {delivered}，共 {pages} 页。"
                        "请用 resume_preview 补齐相关页后再 accept。",
                        retryable=True,
                    )
                if not notes:
                    raise ToolError(
                        "VISUAL_RECORD_MISSING",
                        "vision 已启用：accept 必须带 visual_notes（看图后的结构化结论）。",
                        retryable=True,
                    )
            delivery = self._write_delivery_report(
                request.artifact_id, rev, record, notes=notes
            )
            self.store.accept_revision(request.artifact_id, index, record,
                                       notes=notes, visual_status=delivery['visual']['status'])
            data = {
                "accepted_revision": rev,
                "docx": record["docx"],
                "pdf": record.get("pdf"),
                "page_count": record.get("page_count"),
                "visual": delivery["visual"]["status"],
                "notes": notes,
                "delivery_report": delivery["report_ref"],
            }
            # 幂等登记与版本提交在同一把 artifact 锁内完成：否则「提交成功但
            # 记录尚不可见」的窗口会让并发重试重复执行。
            self.store.record_request(
                request.request_id,
                payload_sig,
                {
                    "kind": "accept",
                    "artifact_id": request.artifact_id,
                    "revision": rev,
                    "data": data,
                },
            )
        return OperationResult(
            ok=True,
            status="validated",
            artifact_id=request.artifact_id,
            revision=rev,
            data=data,
            next_action="交付或继续 resume_repair_v2 调整。",
        )

    def _write_delivery_report(
        self, artifact_id: str, revision: int, record: dict[str, Any], *, notes: str
    ) -> dict[str, Any]:
        """从**被接受版本自身**生成完整、同版的交付报告。

        同时把 `work/qa/resume.json`（Runtime 交付硬门读取的文件）重写成该
        版本的机械/视觉状态——旧版接受不得污染新版 QA 记录。
        """
        mech = record.get("mechanical", {})
        visual = record.get("visual", {})
        report = {
            "artifact_id": artifact_id,
            "revision": revision,
            "content_hash": record.get("content_hash"),
            "page_count": record.get("page_count"),
            "mechanical": {
                "passed": bool(mech.get("passed")),
                "errors": list(mech.get("errors") or []),
            },
            "visual": {
                "status": "passed" if notes else "not_run",
                "delivered_pages": sorted(
                    int(p) for p in visual.get("delivered_pages", [])
                ),
                "notes": notes,
            },
            "layout_mode": record.get("layout_mode"),
            "header": record.get("header") or {},
            "docx": record.get("docx"),
            "pdf": record.get("pdf"),
            "docx_sha256": record.get("docx_sha256"),
            "pdf_sha256": record.get("pdf_sha256"),
            "renderer": record.get("renderer"),
        }
        report_path = self.store.revision_dir(artifact_id, revision) / "delivery_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        qa_path = self.workspace / "work" / "qa" / "resume.json"
        qa_path.parent.mkdir(parents=True, exist_ok=True)
        qa_payload = {
            "mechanical": "passed" if report["mechanical"]["passed"] else "failed",
            "mechanical_issues": report["mechanical"]["errors"],
            "visual": report["visual"]["status"],
            "visual_notes": notes,
            "template": self.template_id,
            "revision": revision,
            "accepted_revision": revision,
            "checks_run": record.get("checks_run", []),
        }
        with self.store.request_lock():   # 共享文件：与请求索引同一把工作区状态锁
            atomic_write_json(qa_path, qa_payload)
        return {"report_ref": self._rel(report_path), "visual": report["visual"]}

    def restore(self, request: Any) -> OperationResult:
        payload_sig = self._request_signature(
            "restore",
            {
                "artifact_id": request.artifact_id,
                "target_revision": int(request.target_revision),
                "expected_accepted_revision": int(request.expected_accepted_revision),
            },
        )
        replayed = self._replay_or_none(request.request_id, payload_sig)
        if replayed is not None:
            return replayed
        with self.store.artifact_lock(request.artifact_id):
            replayed = self._replay_or_none(request.request_id, payload_sig, reserve=True)
            if replayed is not None:
                return replayed
            index = self.store.index(request.artifact_id)
            if int(request.expected_accepted_revision) != int(index.get("accepted") or 0):
                raise ToolError(
                    "VERSION_CONFLICT",
                    f"expected_accepted_revision={request.expected_accepted_revision} "
                    f"≠ 当前 accepted={index.get('accepted')}。",
                    retryable=True,
                )
            target = self.store.load_revision(request.artifact_id, int(request.target_revision))
            new_rev = int(index["current"]) + 1
            return self._create_revision(
                artifact_id=request.artifact_id,
                content=target["content"],
                base_revision=int(index["current"]),
                request_id=request.request_id or f"restore-{request.target_revision}",
                layout_mode=target.get("layout_mode", "reflow"),
                changes=[{"op": "restore", "from_revision": int(request.target_revision)}],
                force_revision=new_rev,
                request_payload=payload_sig,
                request_kind="revision",
            )

    def preview(self, request: Any) -> OperationResult:
        record = self.store.load_revision(request.artifact_id, int(request.revision))
        pngs = sorted((self.store.revision_dir(request.artifact_id, int(request.revision))
                       / "render").glob("page-*.png"))
        want = [int(p) for p in request.pages] or list(range(1, len(pngs) + 1))
        refs = [self._rel(p) for p in pngs if int(p.stem.split("-")[1]) in want]
        if not refs:
            raise ToolError("STALE_PREVIEW", f"revision {request.revision} 无这些页的图像")
        refs, images, total, remaining = self._collect_preview(refs)
        record = self._record_delivery(
            request.artifact_id, int(request.revision), refs, images
        )
        return OperationResult(
            ok=True, status="validated", artifact_id=request.artifact_id,
            revision=int(request.revision), preview_refs=refs, images=images,
            data={
                "pages": want,
                "bytes": total,
                "remaining_pages": remaining,
                "delivered_pages": record.get("visual", {}).get("delivered_pages", []),
                "visual": record.get("visual", {}).get("status", "pending"),
            },
        )

    # ---------- 内部：版本创建 ----------

    def _create_revision(
        self,
        *,
        artifact_id: str,
        content: dict[str, Any],
        base_revision: int | None,
        request_id: str,
        layout_mode: str,
        changes: list[dict[str, Any]],
        force_revision: int | None = None,
        request_payload: str | None = None,
        request_kind: str = "revision",
    ) -> OperationResult:
        """在**调用方已持锁、且已完成请求级幂等判定**后创建新版本。

        幂等判定被有意上移到 generate/repair/restore 入口：那里才能拿到
        「与执行结果无关」的请求级签名（否则重试会因版本已推进而先撞版本检查）。
        """
        # 无效强调在创建候选、启动 Word 前拒绝，不留下无法恢复的半个版本。
        for section in content.get("sections", []):
            for entry in section.get("entries", []):
                if entry.get("highlights"):
                    self._paragraph_styles(section, canonical_entry(section, entry))
        index, revision, rev_dir = self.store.reserve_revision(artifact_id, force_revision)
        record = self._render_revision(
            artifact_id, revision, content, layout_mode=layout_mode,
            base_revision=base_revision, changes=changes, rev_dir=rev_dir,
        )
        self.store.publish_revision(artifact_id, index, revision)
        if request_id:
            self.store.record_request(
                request_id,
                request_payload or self._request_signature(
                    "revision", {"content": content, "changes": changes,
                                 "layout_mode": layout_mode}
                ),
                {
                    "kind": request_kind,
                    "artifact_id": artifact_id,
                    "revision": revision,
                },
            )
        return self._result_from_record(artifact_id, record, idempotent=False)

    def _render_revision(
        self,
        artifact_id: str,
        revision: int,
        content: dict[str, Any],
        *,
        layout_mode: str,
        base_revision: int | None,
        changes: list[dict[str, Any]],
        rev_dir: Path,
    ) -> dict[str, Any]:
        from skill_toolbox.resume_layout import renderer

        scenario = self._scenario_from_content(content)
        scenario['header'] = self._header_payload(content)
        built = renderer.build(scenario, content, rev_dir,
            template=self._template_dir(self.template_id) / 'template.docx', profile=self.profile,
            run_stage=self._run_stage, layout_mode=layout_mode)
        plan, qa_report = built.plan, built.qa
        docx, pdf, pngs = built.docx, built.pdf, built.pages
        single_page_fit = built.single_page_fit
        record = {
            "artifact_id": artifact_id,
            "revision": revision,
            "parent": base_revision,
            "layout_mode": layout_mode,
            "changes": changes,
            "content": content,
            "content_hash": _payload_hash(content),
            "single_page_fit": single_page_fit,
            "page_count": plan.pages,
            "mechanical": {
                "passed": bool(qa_report.get("passed")),
                "errors": [
                    i["detail"] for i in qa_report.get("issues", [])
                    if i.get("severity") == "error"
                ],
            },
            "visual": {"status": "pending", "delivered_pages": []},
            "header": built.header,
            "docx": self._rel(docx),
            "pdf": self._rel(pdf) if pdf.is_file() else None,
            "pages": [self._rel(p) for p in pngs],
            "docx_sha256": sha256_file(docx),
            "pdf_sha256": sha256_file(pdf) if pdf.is_file() else None,
            "instance_model": self._instance_model(plan, content),
            "renderer": f"{_pdf_creator(pdf)} PDF + pdftoppm 120dpi",
        }
        self.store.save_revision(artifact_id, revision, record)
        # QAReport（Runtime 的机械硬门读这里）：机械结果来自本版真实 QA；
        # 视觉状态按实际投递记录，未投递即 pending/not_run，不冒充视觉验收。
        self._write_qa_report(revision, qa_report)
        return record

    def _write_qa_report(
        self, revision: int, qa_report: dict[str, Any], *, visual: str | None = None
    ) -> None:
        """写 work/qa/resume.json（Runtime finish 硬门 + 前端状态）。

        QAReport.visual 只接受 passed/failed/not_run（material_models）：
        生成/修复阶段尚未完成视觉确认 → not_run；accept（agent 已看完该版
        全部页面并给出结论）→ passed。
        """
        qa_dir = self.workspace / "work" / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "mechanical": "passed" if qa_report.get("passed") else "failed",
            "mechanical_issues": [
                i["detail"] for i in qa_report.get("issues", [])
                if i.get("severity") == "error"
            ],
            "visual": visual or "not_run",
            "template": self.template_id,
            "revision": revision,
            "checks_run": qa_report.get("checks_run", []),
        }
        path = qa_dir / "resume.json"
        with self.store.request_lock():   # 共享文件：与请求索引同一把工作区状态锁
            atomic_write_json(path, payload)

    def _result_from_record(
        self, artifact_id: str, record: dict[str, Any], *, idempotent: bool
    ) -> OperationResult:
        pages = record.get("pages") or []
        refs, images, total, remaining = self._collect_preview(pages)
        record = self._record_delivery(
            artifact_id, int(record["revision"]), refs, images
        )
        mech = record.get("mechanical", {})
        if not mech.get("passed"):
            # 候选可渲染但机械失败：可作修复输入，禁止接受/交付
            return OperationResult(
                ok=False, status="failed", artifact_id=artifact_id,
                revision=record["revision"], preview_refs=refs, images=images,
                issues=[{"code": "MECHANICAL_FAILED", "message": e}
                        for e in mech.get("errors", [])],
                data={"candidate_revision": record["revision"], "usable": False,
                      "page_count": record.get("page_count"),
                      "remaining_pages": remaining},
                next_action="按 issues 修正内容后重试；该候选不可接受、不可交付。",
            )
        return OperationResult(
            ok=True,
            status="validated",
            artifact_id=artifact_id,
            revision=record["revision"],
            preview_refs=refs,
            images=images,
            data={
                "candidate_revision": record["revision"],
                "parent_revision": record.get("parent"),
                "page_count": record.get("page_count"),
                "actual_changes": record.get("changes", []),
                "affected_pages": sorted(
                    {int(Path(p).stem.split("-")[1]) for p in pages}
                ),
                "visual": record.get("visual", {}).get("status", "pending"),
                "delivered_pages": record.get("visual", {}).get("delivered_pages", []),
                "remaining_pages": remaining,
                "preview_bytes": total,
                "idempotent_replay": idempotent,
                "docx": record.get("docx"),
                "pdf": record.get("pdf"),
                "checks": ["content_completeness", "page_count", "within_page",
                           "no_overlap", "title_uniqueness",
                           "measurement_vs_render", "no_extra_rendered_content"],
            },
            next_action=(
                "看图检查版式；有异议用 resume_repair_v2 继续调整，满意则 "
                "resume_accept(candidate_revision, expected_accepted_revision)。"
                + (" 注意：本次预览未覆盖全部页面，accept 前先用 resume_preview 补看 "
                   f"remaining_pages={remaining}。" if remaining else "")
            ),
        )

    # ---------- 内部：内容 ↔ 场景 ----------

    def _header_payload(self, content: dict[str, Any]) -> dict[str, Any]:
        """把 v2 `header` 转成 emit 需要的载荷；**未知字段显式拒绝**。"""
        header = content.get("header") or {}
        fields = {str(k): str(v) for k, v in (header.get("fields") or {}).items()}
        out: dict[str, Any] = {}
        custom = [dict(field) for field in header.get("custom_fields") or []]
        keys = [field["key"] for field in custom]
        if len(keys) != len(set(keys)):
            raise ToolError("CONTENT_INVALID", "custom_fields 的 key 不得重复")
        out["custom_fields"] = custom
        out["hidden_fields"] = list(header.get("hidden_fields") or [])
        if fields:
            unknown = sorted(set(fields) - set(self.profile.header_labels.values()))
            if unknown:
                raise ToolError(
                    "FIELD_UNSUPPORTED",
                    f"header.fields 不支持的键: {unknown}。"
                    f"{self.template_id} 个人信息区可用键: {sorted(set(self.profile.header_labels.values()))}。",
                    suggestion="只用 resume_prepare_v2 返回的 header_fields；"
                               "未支持的键不会被静默忽略。",
                )
            out["fields"] = fields
        photo_path = str(header.get("photo_path") or "").strip()
        out["hide_photo"] = bool(header.get("hide_photo", False))
        if photo_path and not out["hide_photo"]:
            path = (self.workspace / photo_path).resolve()
            try:
                path.relative_to(self.workspace)
            except ValueError:
                raise ToolError(
                    "ASSET_UNKNOWN",
                    f"photo_path 必须位于 workspace 内: {photo_path}",
                ) from None
            if not path.is_file():
                raise ToolError(
                    "ASSET_UNKNOWN",
                    f"photo_path 不存在: {photo_path}",
                    suggestion="传入 workspace 相对路径（如 sources/photo.png）。",
                )
            out["photo_bytes"] = _normalize_photo(path)
            out["photo_part"] = self.profile.photo_part
        return out


    def _scenario_from_content(self, content: dict[str, Any]) -> dict[str, Any]:
        sections = []
        for sec in content.get("sections", []):
            entries = [
                {"id": e["id"], "text": self._entry_text(sec, e),
                 "head": dict(e.get("head") or {}),
                 "has_heading": bool(self._entry_heading(sec, e)),
                 "heading_lines": len(self._entry_heading(sec, e).splitlines()),
                 "tech_stack_line": len(self._entry_heading(sec, e).splitlines()) if e.get("tech_stack") else None,
                 "detail_styles": {len(self._entry_heading(sec, e).splitlines()) + bool(e.get("tech_stack")) + i: {}
                                   for i, group in enumerate(detail_groups(sec, e))},
                 "paragraph_styles": self._paragraph_styles(sec, e),
                 "inline_styles": title_metrics(sec, e),
                 **{key: e[key] for key in ("width_pt", "font_size_pt", "scale")
                    if e.get(key) is not None}}
                for e in (canonical_entry(sec, raw) for raw in sec.get("entries", []))
            ]
            sections.append({
                "id": sec["key"], "title": sec["title"],
                "prototype": sec.get("prototype", "experience_v1"),
                "entries": entries,
                "scale": sec.get("scale") or 1.0,
                "density": content.get("density", "normal"),
            })
        from skill_toolbox.resume_layout.component_template import prepare_sections
        prepare_sections(sections, self.template_id)
        return {"sections": sections}

    def _paragraph_styles(self, section, entry):
        from skill_toolbox.resume_layout.highlights import compile_highlights
        heading = self._entry_heading(section, entry)
        start = len(heading.splitlines()) + bool(entry.get("tech_stack"))
        groups = detail_groups(section, entry)
        order = ("org", "role") if is_project(section) else self.profile.header_slot_order
        result = compile_highlights(entry, heading, order, start + len(groups))
        for index, group in enumerate(groups, start):
            offset = 0
            for field in group:
                text = f"{field['label']}：{field['value']}"
                result.setdefault(index, []).append({**field, "start": offset, "end": offset + len(text)})
                offset += len(text) + 3  # ' · '
        return result


    def _entry_text(self, section: dict[str, Any], entry: dict[str, Any]) -> str:
        entry = canonical_entry(section, entry)
        bullets = [b for b in (entry.get("bullets") or []) if str(b).strip()]
        lines = [ln for ln in (entry.get("lines") or []) if str(ln).strip()]
        header = self._entry_heading(section, entry)
        parts = [header] if header else []
        if entry.get("tech_stack"):
            stack = entry["tech_stack"]
            stack = stack if stack.startswith(("技术栈：", "技术栈:")) else "技术栈：" + stack
            parts.append(stack)
        parts.extend(" · ".join(f"{field['label']}：{field['value']}" for field in group)
                     for group in detail_groups(section, entry))
        return "\n".join(parts + (bullets or lines))

    def _entry_heading(self, section, entry):
        entry = canonical_entry(section, entry)
        head = dict(entry.get("head") or {})
        if is_project(section):
            # 日期保留在内容数据中；项目标题连续排布，由 Word 按真实宽度换行。
            slots = [head.get("org", ""), *[f['text'] for f in title_metrics(section, entry)],
                     head.get("role", "")]
            return " · ".join(str(slot).strip() for slot in slots if str(slot).strip())
        return "\t".join(str(head[key]).strip() for key in self.profile.header_slot_order
                         if str(head.get(key) or "").strip())

    # ---------- 内部：编辑动作 ----------


    # ---------- 内部：几何动作落地 ----------


    # ---------- 内部：实例模型与预览 ----------

    def _instance_model(self, plan: Any, content: dict[str, Any]) -> list[dict[str, Any]]:
        protos = {s["key"]: s.get("prototype", "experience_v1")
                  for s in content.get("sections", [])}
        model = []
        for sec in plan.sections:
            section_content = next(s for s in content["sections"] if s["key"] == sec.section_id)
            widths = {e["id"]: e.get("width_pt") for e in section_content["entries"]}
            body_width = self.profile.body_width_pt
            model.append({
                "section_key": sec.section_id,
                "title": sec.title,
                "page_index": sec.page_index,
                "anchor_y_pt": round(sec.anchor_y_pt, 2),
                "prototype": protos.get(sec.section_id, "experience_v1"),
                "entries": [
                    {
                        "instance_id": f"{sec.section_id}#{e.instance_id}",
                        "page_index": e.page_index,
                        "region": {
                            "x_pt": e.body_x_pt if e.body_x_pt is not None else self.profile.body_x_pt,
                            "y_pt": round(e.anchor_y_pt, 2),
                            "w_pt": getattr(e, "body_width_pt", None) or widths.get(e.instance_id) or body_width,
                            "h_pt": round(e.body_h_pt, 2),
                        },
                        "lines": e.measured_lines,
                        "text_height_pt": round(e.text_height_pt, 2),
                    }
                    for e in sec.entries
                ],
            })
        return model

    def _collect_preview(
        self, pages: list[str]
    ) -> tuple[list[str], list[dict[str, Any]], int, list[int]]:
        """按预算投递预览图：返回 (投递的 refs, 内联图像, 字节数, 未投递页码)。

        超预算/超单图上限的页**不投递**，进入 remaining_pages，由 agent 用
        resume_preview 补看；未补看前 accept 会被 VISUAL_PENDING 拦住。
        """
        budget = list(pages[:IMAGE_BUDGET_PAGES])
        overflow = [
            int(Path(p).stem.split("-")[1]) for p in pages[IMAGE_BUDGET_PAGES:]
        ]
        refs: list[str] = []
        images: list[dict[str, Any]] = []
        total = 0
        if not self.vision:
            # 无 vision：不内联图像（Provider 收不了），只给可展示的路径 +
            # 全部页计入 remaining_pages —— 绝不能因此声称视觉覆盖。
            return [], [], 0, sorted(
                {int(Path(p).stem.split("-")[1]) for p in pages} | set(overflow)
            )
        for ref in budget:
            path = self.workspace / ref
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > IMAGE_MAX_BYTES or total + size > IMAGE_TOTAL_BYTES:
                overflow.append(int(Path(ref).stem.split("-")[1]))
                continue
            total += size
            refs.append(ref)
            images.append({
                "media_type": "image/png",
                "base64_data": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
        return refs, images, total, sorted(set(overflow))

    def _record_delivery(
        self,
        artifact_id: str,
        revision: int,
        refs: list[str],
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把「本次真正投递给模型的页」写回 revision 的视觉覆盖记录。

        只记录**内联进模型视觉输入**（images）的页：路径文本不算视觉覆盖。
        """
        delivered = sorted({int(Path(ref).stem.split('-')[1]) for ref in refs}) if any(images) else []
        return self.store.record_delivery(artifact_id, revision, delivered)
