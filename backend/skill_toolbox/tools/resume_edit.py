"""简历 v2 编辑服务：编排版本、布局、渲染与候选验收。"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.tools.workspace import atomic_write_json, sha256_file
from skill_toolbox.tools.resume_store import ResumeStore
from skill_toolbox.resume_content import canonical_entry, is_project, inline_metrics, detail_rows

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


V2_TEMPLATE_ID = "t109"
V2_ARCHIVE_REL = "work/resume/spacing_archive.json"
# 图像预算（初始值；真实 provider 探针后固化 —— 见 SCHEMA_FREEZE §5）
IMAGE_BUDGET_PAGES = 3
IMAGE_MAX_BYTES = 2 * 1024 * 1024
IMAGE_TOTAL_BYTES = 4 * 1024 * 1024
V2_MIN_ENTRY_GAP_PT = 2.0
V2_MIN_VISIBLE_GAP_PT = 8.0
# 正文框宽度边界（模板正文列宽 532.5pt 为上限；过窄会无法排版）
MIN_BODY_W_PT = 200.0
MAX_BODY_W_PT = 532.5
# 正文框高度上限（不越过页容量）
MAX_BODY_H_PT = 640.0
# 模板照片媒体部件（t109 照片 anchor 组合 12 的 rId4 → media/image1.png）
V2_PHOTO_PART = "word/media/image1.png"


def _payload_hash(payload: object) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class _GapOverride:
    """在档案之上叠加内容层显式声明的条目间距（未声明则回落档案/默认）。"""

    def __init__(self, archive: object, overrides: dict[str, float], section_delta: float | None = None) -> None:
        self._archive = archive
        self._overrides = overrides
        self._section_delta = section_delta

    def delta_after(self, prev_id: str, next_id: str | None, default: float) -> float:
        if self._section_delta is not None:
            return self._section_delta
        return self._archive.delta_after(prev_id, next_id, default)  # type: ignore[attr-defined]

    def entry_gap_for(self, section_id: str, default: float) -> float:
        if section_id in self._overrides:
            return float(self._overrides[section_id])
        return self._archive.entry_gap_for(section_id, default)  # type: ignore[attr-defined]


class ResumeEditService:
    """t109 v2：prepare / generate / repair / accept / restore / preview。"""

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
        self.template_id = template_id
        self.geometry = self.profile.geometry
        self.archive_rel = V2_ARCHIVE_REL if template_id == "t109" else f"work/resume/{template_id}_spacing_archive.json"
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
                "其他模板请走 legacy 字段填充路线。",
            )
        return tpl

    def _layout_modules(self):
        from skill_toolbox.resume_layout import pipeline as RG
        from skill_toolbox.resume_layout import layout as RL
        from skill_toolbox.resume_layout import report as RR
        from skill_toolbox.resume_layout import spacing as RS

        return RG, RL, RR, RS

    def _archive(self):
        from skill_toolbox.resume_layout import spacing
        path = self.workspace / self.archive_rel
        self._run_measurement(None, path.parent)
        return spacing.load_archive(path)

    def _run_measurement(self, scenario: dict | None, work: Path) -> None:
        """COM 测量和首次间距探测由受控子进程执行。"""
        work.mkdir(parents=True, exist_ok=True)
        spec = work / "measure_input.json"
        if scenario is not None:
            payload = {"sections": scenario["sections"]}
            header = scenario.get("header") or {}
            components = {key: header[key] for key in ("fields", "hidden_fields", "custom_fields")
                          if header.get(key)}
            if components:
                payload["header"] = components
            atomic_write_json(spec, payload)
        self._run_stage(
            [sys.executable, "-m", "skill_toolbox.resume_layout.pipeline",
             str(self._template_dir(self.template_id) / "template.docx"),
             str(spec) if scenario is not None else "-", str(work),
             str(self.workspace / self.archive_rel), self.template_id],
            "MEASURE_FAILED",
        )

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

    def repair(self, request: Any) -> OperationResult:
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
            new_content, records = self._apply_changes(content, request.changes)
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
            index["accepted"] = rev
            index.setdefault("accepted_log", []).append(
                {"revision": rev, "visual": delivery["visual"]["status"], "notes": notes}
            )
            self.store.save_index(request.artifact_id, index)
            record["accepted"] = True
            record.setdefault("visual", {})["notes"] = notes
            record["visual"]["status"] = (
                "recorded" if notes else visual.get("status", "not_run")
            )
            (self.store.revision_dir(request.artifact_id, rev) / "revision.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
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
        art_dir = self.store.artifact_dir(artifact_id)
        art_dir.mkdir(parents=True, exist_ok=True)
        index_path = art_dir / "index.json"
        index = (
            json.loads(index_path.read_text(encoding="utf-8"))
            if index_path.is_file()
            else {"current": 0, "accepted": 0}
        )
        revision = int(force_revision or index["current"] + 1)
        rev_dir = self.store.revision_dir(artifact_id, revision)
        if rev_dir.exists() and (rev_dir / "revision.json").is_file():
            # 不可变版本：绝不复用已存在的版本号（恢复/并发保护）
            raise ToolError(
                "REVISION_EXISTS",
                f"{artifact_id} 的 revision {revision} 已存在；版本不可覆盖，请重试。",
                retryable=True,
            )
        rev_dir.mkdir(parents=True, exist_ok=True)
        record = self._render_revision(
            artifact_id, revision, content, layout_mode=layout_mode,
            base_revision=base_revision, changes=changes, rev_dir=rev_dir,
        )
        index["current"] = revision
        self.store.save_index(artifact_id, index)
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
        import shutil

        RG, RL, RR, RS = self._layout_modules()
        scenario = self._scenario_from_content(content)
        header_info = self._header_payload(content)
        if header_info:
            scenario["header"] = header_info
        work = rev_dir / "build"
        work.mkdir(parents=True, exist_ok=True)
        self._run_measurement(scenario, work)
        header_fit = work / "header_fit.json"
        if header_fit.is_file():
            scenario.setdefault("header", {})["fit"] = json.loads(header_fit.read_text(encoding="utf-8"))
        measurements = json.loads((work / "measure_result.json").read_text(encoding="utf-8"))
        measured = {
            r["entry_id"]: RL.MeasureResult.from_dict(r)
            for r in measurements["results"]
        }
        archive = RS.load_archive(self.workspace / self.archive_rel)
        overrides = {
            # v2 内容用 section.key（不是 legacy 的 id）：读错键会在任何带
            # entry_gap_pt 的请求上抛 KeyError，把可渲染的请求变成工具异常。
            str(s.get("key") or s.get("id")): float(s["entry_gap_pt"])
            for s in content.get("sections", [])
            if s.get("entry_gap_pt") is not None
        }
        gap_source = _GapOverride(archive, overrides) if overrides else archive
        if content.get("density") == "compact":
            gaps = {s["key"]: 3.0 for s in content["sections"]}
            gap_source = _GapOverride(archive, {**gaps, **overrides}, section_delta=8.0)
        # 几何约束按模式处理（P2-R2）：
        # - reflow：交给完整 flow 排版器（entry_adjust），后续条目/栏目/分页
        #   一起重排；
        # - local：先常规排版，再只平移目标条目，并做跨栏目碰撞检查。
        adjust = self._geometry_adjust(content) if layout_mode == "reflow" else None
        try:
            plan = RL.plan_layout(
                scenario["sections"], measured, spacing=gap_source,
                min_visible_gap_pt=V2_MIN_VISIBLE_GAP_PT,
                entry_adjust=adjust, geometry=self.geometry,
            )
        except RL.LayoutUnsatisfiable as exc:
            raise ToolError(
                "LAYOUT_UNSATISFIABLE", str(exc), retryable=True,
                suggestion="精简超长条目或拆分为多条；不要删内容绕过检查。",
            ) from None
        if layout_mode == "local":
            plan = self._apply_local_geometry(plan, content, RL)
        else:
            self._validate_plan_geometry(plan, RL)
        single_page_fit = None
        if layout_mode == "reflow" and plan.pages > 1:
            unpaged = RL.plan_layout(scenario["sections"], measured, spacing=gap_source,
                                    min_visible_gap_pt=V2_MIN_VISIBLE_GAP_PT, entry_adjust=adjust,
                                    geometry=replace(self.geometry, page_bottom_pt=1_000_000))
            bottom = max(e.anchor_y_pt + e.body_h_pt for s in unpaged.sections for e in s.entries)
            reduction = max(0, bottom - self.geometry.page_bottom_pt)
            pitch = min(row["line_pitch_pt"] for row in measurements["results"])
            single_page_fit = {"required_reduction_pt": round(reduction, 1),
                               "approx_lines_to_save": math.ceil(reduction / pitch),
                               "reference_line_pitch_pt": pitch}
        docx = rev_dir / "resume.docx"
        emit_info = RG.emit_scenario(scenario, plan, docx,
                                     template=self._template_dir(self.template_id) / "template.docx", template_id=self.template_id)
        render_dir = rev_dir / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        from skill_toolbox.resume_layout.t109 import RENDER_SCRIPT
        self._run_stage(
            [sys.executable, str(RENDER_SCRIPT), str(docx), str(render_dir)], "RENDER_FAILED"
        )
        # 使用本版真实渲染边界收紧首屏，再复用测量结果重排；续页仍遵循模板母版。
        from skill_toolbox.resume_layout.header import body_start_from_render
        header_components = (emit_info.get("header") or {}).get("fields")
        if header_components is None:
            raise ValueError("HEADER_CONTRACT_MISSING: 模板必须返回个人信息组件")
        body_top = body_start_from_render(render_dir / "resume.pdf", header_components,
                                          self.geometry.page_top_pt)
        if abs(body_top - self.geometry.page_top_pt) > 1:
            dynamic_geometry = replace(self.geometry, page_top_pt=body_top,
                                       continuation_page_top_pt=self.geometry.continuation_page_top_pt or self.geometry.page_top_pt)
            plan = RL.plan_layout(scenario["sections"], measured, spacing=gap_source,
                                  min_visible_gap_pt=V2_MIN_VISIBLE_GAP_PT,
                                  entry_adjust=adjust, geometry=dynamic_geometry)
            if layout_mode == "local":
                plan = self._apply_local_geometry(plan, content, RL)
            self._validate_plan_geometry(plan, RL)
            emit_info = RG.emit_scenario(scenario, plan, docx,
                template=self._template_dir(self.template_id) / "template.docx", template_id=self.template_id)
            for stale in render_dir.glob("page-*.png"):
                stale.unlink()
            self._run_stage([sys.executable, str(RENDER_SCRIPT), str(docx), str(render_dir)], "RENDER_FAILED")
            single_page_fit = None
            if layout_mode == "reflow" and plan.pages > 1:
                unpaged = RL.plan_layout(scenario["sections"], measured, spacing=gap_source,
                    min_visible_gap_pt=V2_MIN_VISIBLE_GAP_PT, entry_adjust=adjust,
                    geometry=replace(dynamic_geometry, page_bottom_pt=1_000_000))
                reduction = max(0, max(e.anchor_y_pt + e.body_h_pt for s in unpaged.sections for e in s.entries) - self.geometry.page_bottom_pt)
                pitch = min(row["line_pitch_pt"] for row in measurements["results"])
                single_page_fit = {"required_reduction_pt": round(reduction, 1),
                    "approx_lines_to_save": math.ceil(reduction / pitch), "reference_line_pitch_pt": pitch}
        (rev_dir / "layout_plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        measured_view = {
            r["entry_id"]: r
            for r in measurements["results"]
        }
        header_values = [
            info["after"] for info in (emit_info.get("header") or {}).get("fields", {}).values()
        ]
        header_components = (emit_info.get("header") or {}).get("fields")
        if header_components is None:
            raise ValueError("HEADER_CONTRACT_MISSING: 组件模板必须返回 header.fields（含 column），供共享对齐验收；无字段时显式返回空字典。")
        qa_report = RR.qa_scenario(
            rev_dir, scenario, plan.to_dict(), measured_view, pdf_name="resume",
            header_values=header_values, template_id=self.template_id,
            template=self._template_dir(self.template_id) / "template.docx",
            header_components=header_components,
        )
        (rev_dir / "qa_report.json").write_text(
            json.dumps(qa_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        pdf = render_dir / "resume.pdf"
        pngs = sorted(render_dir.glob("page-*.png"))
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
            "header": emit_info.get("header") or {},
            "docx": self._rel(docx),
            "pdf": self._rel(pdf) if pdf.is_file() else None,
            "pages": [self._rel(p) for p in pngs],
            "docx_sha256": sha256_file(docx),
            "pdf_sha256": sha256_file(pdf) if pdf.is_file() else None,
            "instance_model": self._instance_model(plan, content),
            "renderer": "Word COM ExportAsFixedFormat + pdftoppm 120dpi",
        }
        shutil.rmtree(work, ignore_errors=True)
        (rev_dir / "revision.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
            "visual": visual or ("not_run" if not self.vision else "not_run"),
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

    @staticmethod
    def _next_entry_id(section: dict[str, Any]) -> str:
        """自动生成不冲突的条目 id（`<栏目key>-<n>`），并保证全局稳定可复现。"""
        used = {str(e.get("id")) for e in section.get("entries", [])}
        n = len(used) + 1
        while f"{section['key']}-{n}" in used:
            n += 1
        return f"{section['key']}-{n}"

    def _scenario_from_content(self, content: dict[str, Any]) -> dict[str, Any]:
        sections = []
        for sec in content.get("sections", []):
            entries = [
                {"id": e["id"], "text": self._entry_text(sec, e),
                 "has_heading": bool(self._entry_heading(sec, e)),
                 "heading_lines": len(self._entry_heading(sec, e).splitlines()),
                 "tech_stack_line": len(self._entry_heading(sec, e).splitlines()) if e.get("tech_stack") else None,
                 "detail_styles": {len(self._entry_heading(sec, e).splitlines()) + bool(e.get("tech_stack")) + i: field
                                   for i, field in enumerate(detail_rows(sec, e))},
                 "inline_styles": [{**field, "text": f"{field['label']}：{field['value']}"}
                                   for field in inline_metrics(sec, e)],
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
        if self.template_id == "t001":
            from skill_toolbox.resume_layout.t001 import SECTIONS, source_key
            for sec in sections:
                sec["body_offset_pt"] = SECTIONS[source_key(sec)][2]
        return {"sections": sections}

    def _head_pattern(self, prototype: str) -> str:
        """原型条目标题段的**原件原文**（列位基准；运行时从原件 XML 读取）。"""
        from skill_toolbox.resume_layout import emit

        if self.template_id == "t001":
            from skill_toolbox.resume_layout.t001 import body_for
            root = emit.load_document_xml(self._template_dir(self.template_id) / "template.docx")
            tx = body_for(root, {"id": "work" if prototype == "experience_v1" else "skills"}).find(
                ".//" + emit.W + "txbxContent")
            return "".join(next(tx.iter(emit.W + "p")).itertext())
        anchor_name = {"experience_v1": "组合 216", "plain_lines_v1": "组合 219"}.get(
            prototype
        )
        if not anchor_name:
            return ""
        root = emit.load_document_xml(self._template_dir(self.template_id) / "template.docx")
        anchor = emit._anchor_by_docpr_name(root, anchor_name)
        if anchor is None:
            return ""
        tx = emit.find_body_wsp(anchor).find(".//" + emit.W + "txbxContent")
        first = list(tx.iter(emit.W + "p"))[0]
        return "".join(t.text or "" for t in first.iter(emit.W + "t"))

    @staticmethod
    def _display_width(text: str) -> int:
        """仅用于调整槽位间隔的宽度估计（CJK=2，其余=1）；不用于高度测量。"""
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    def _entry_text(self, section: dict[str, Any], entry: dict[str, Any]) -> str:
        entry = canonical_entry(section, entry)
        bullets = [b for b in (entry.get("bullets") or []) if str(b).strip()]
        lines = [ln for ln in (entry.get("lines") or []) if str(ln).strip()]
        header = self._entry_heading(section, entry)
        parts = [header] if header else []
        if entry.get("tech_stack"):
            stack = entry["tech_stack"]
            parts.append(stack if stack.startswith(("技术栈：", "技术栈:")) else "技术栈：" + stack)
        parts.extend(f"{field['label']}：{field['value']}" for field in detail_rows(section, entry))
        return "\n".join(parts + (bullets or lines))

    def _entry_heading(self, section, entry):
        entry = canonical_entry(section, entry)
        head = dict(entry.get("head") or {})
        metrics = inline_metrics(section, entry)
        if metrics:
            head["role"] = " · ".join(f"{f['label']}：{f['value']}" for f in metrics) + (
                " ｜ " + head["role"] if head.get("role") else "")
        order = ("org", "role", "date") if is_project(section) else self.profile.header_slot_order
        slots = [str(head.get(key) or "").strip() for key in order]
        slots = [slot for slot in slots if slot]
        if not is_project(section):
            return "\t".join(slots)
        if len(slots) < 2:
            return "".join(slots)
        scale = float(section.get("scale") or 1) * float(entry.get("scale") or 1)
        font = float(entry.get("font_size_pt") or (10 if self.template_id == "t001" else 10.5)) * scale
        width = float(entry.get("width_pt") or self.profile.body_width_pt * scale) - 14.4 * scale
        # 仅用保守估算选择单行/分段；换行数和高度仍由 Word 实测。
        sizes = [self._display_width(slot) * font * 0.58 for slot in slots]
        fits = sum(sizes) + 12 <= width
        if len(sizes) == 3:
            fits = fits and 2 * max(sizes[0], sizes[2]) + sizes[1] + 12 <= width
        elif self.template_id == "t109":
            fits = fits and sizes[0] < width * 0.58 and sizes[1] < width * 0.4
        separator = "\t" if fits else (" ｜ " if is_project(section) else "\n")
        return separator.join(slots)

    # ---------- 内部：编辑动作 ----------

    def _apply_changes(
        self, content: dict[str, Any], changes: list[Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        import copy

        new = copy.deepcopy(content)
        records: list[dict[str, Any]] = []
        for change in changes:
            op = change.op
            if op == "format_component":
                values = {key: getattr(change, key) for key in ("font_size_pt", "scale")
                          if getattr(change, key) is not None}
                if not values:
                    raise ToolError("CONTENT_INVALID", "格式调整需要 font_size_pt 或 scale。")
                if change.instance_id or change.entry_id:
                    _, entry = self._locate(new, change)
                    entry.update(values)
                else:
                    sec = self._section(new, change.section_key)
                    if "scale" in values:
                        sec["scale"] = values["scale"]
                    if "font_size_pt" in values:
                        for entry in sec["entries"]:
                            entry["font_size_pt"] = values["font_size_pt"]
                records.append({"op": op, "instance_id": change.instance_id,
                                "section": change.section_key, **values})
            elif op == "update_entry":
                sec, entry = self._locate(new, change)
                if change.entry is not None:
                    # 只覆盖**调用方显式给的**字段：pydantic 的默认值（head={}、
                    # bullets=[]）不代表“清空”，否则「只改 bullets」会把条目标题
                    # 槽整行抹掉（内容完整性检查会因此失败）。
                    provided = set(change.entry.model_fields_set) - {"id"}
                    payload = change.entry.model_dump(exclude_none=True)
                    for key in provided:
                        entry[key] = payload.get(key, entry.get(key))
                records.append({"op": op, "instance_id": change.instance_id,
                                "fields": sorted(set(change.entry.model_fields_set) - {"id"})
                                if change.entry is not None else []})
            elif op == "insert_entry":
                section_key = change.section_key or str(change.after or "").split("#", 1)[0]
                if not section_key:
                    raise ToolError(
                        "CONTENT_INVALID",
                        "insert_entry 需要 section_key（栏目 key，来自 resume_prepare_v2）"
                        "，或用 after=<section>#<entry> 指定插入位置。",
                    )
                sec = self._section(new, section_key)
                if change.entry is None:
                    raise ToolError("CONTENT_INVALID", "insert_entry 必须给 entry")
                entry = change.entry.model_dump(exclude_none=True)
                if not str(entry.get("id") or "").strip():
                    entry["id"] = self._next_entry_id(sec)
                elif any(e["id"] == entry["id"] for e in sec["entries"]):
                    raise ToolError(
                        "CONTENT_INVALID",
                        f"条目 id 已存在: {entry['id']}（省略 id 时后端自动生成）。",
                    )
                idx = self._index_of(sec, change.after) + 1 if change.after else len(sec["entries"])
                sec["entries"].insert(idx, entry)
                records.append({"op": op, "section": sec["key"], "entry_id": entry["id"]})
            elif op == "remove_entry":
                sec, entry = self._locate(new, change)
                sec["entries"] = [e for e in sec["entries"] if e is not entry]
                records.append({"op": op, "instance_id": change.instance_id})
            elif op == "move_entry":
                sec, entry = self._locate(new, change)
                sec["entries"] = [e for e in sec["entries"] if e is not entry]
                if change.before:
                    idx = self._index_of(sec, change.before)
                elif change.after:
                    idx = self._index_of(sec, change.after) + 1
                else:
                    idx = int(change.dy_pt) if change.dy_pt else len(sec["entries"])
                sec["entries"].insert(max(0, min(idx, len(sec["entries"]))), entry)
                records.append({"op": op, "instance_id": change.instance_id})
            elif op == "insert_section":
                if change.section is None:
                    raise ToolError("CONTENT_INVALID", "insert_section 必须给 section")
                sec = change.section.model_dump(exclude_none=True)
                target = change.before or change.after
                idx = len(new["sections"])
                if target:
                    anchor = self._section(new, target)
                    idx = new["sections"].index(anchor) + (0 if change.before else 1)
                new["sections"].insert(idx, sec)
                records.append({"op": op, "section": sec["key"]})
            elif op == "remove_section":
                new["sections"] = [
                    s for s in new["sections"] if s["key"] != change.section_key
                ]
                records.append({"op": op, "section": change.section_key})
            elif op == "update_section_title":
                sec = self._section(new, change.section_key)
                sec["title"] = change.title
                records.append({"op": op, "section": sec["key"], "title": change.title})
            elif op == "set_entry_gap":
                sec = self._section(new, change.section_key)
                gap = float(change.gap_pt or 0.0)
                if gap < V2_MIN_ENTRY_GAP_PT or gap > 24.0:
                    raise ToolError(
                        "BOUNDS_VIOLATION",
                        f"gap_pt={gap} 超出允许范围 [{V2_MIN_ENTRY_GAP_PT}, 24.0]",
                    )
                sec["entry_gap_pt"] = gap
                records.append({"op": op, "section": sec["key"], "gap_pt": gap})
            elif op in {"move_component", "resize_component"}:
                # 几何动作在布局结果上落地（见 _apply_geometry），此处只做参数
                # 校验并记录；**不支持的参数直接拒绝**，不返回成功却未执行。
                sec, entry = self._locate(new, change)
                if op == "move_component":
                    if change.width_pt is not None or change.height_pt is not None \
                            or change.height_delta_pt is not None:
                        raise ToolError(
                            "ACTION_NOT_ALLOWED",
                            "move_component 只接受 dx_pt/dy_pt；"
                            "宽高请用 resize_component（width_pt/height_pt/"
                            "height_delta_pt）。",
                        )
                    if abs(change.dx_pt) > 1e-9 and abs(change.dy_pt) > 1e-9:
                        raise ToolError(
                            "BOUNDS_VIOLATION",
                            "同一条动作禁止同时给 dx_pt 与 dy_pt（计划 §6.1）",
                        )
                    if change.dx_pt:
                        raise ToolError(
                            "ACTION_NOT_ALLOWED",
                            "正文组件不允许横向重定位（会改变文字测量宽度）；"
                            "如需变窄请用 resize_component(width_pt=…)。",
                        )
                    dy = float(change.dy_pt)
                    if not (-120.0 <= dy <= 120.0):
                        raise ToolError("BOUNDS_VIOLATION", f"dy_pt={dy} 超出 [-120, 120]")
                    if abs(dy) < 1e-9:
                        raise ToolError("CONTENT_INVALID", "move_component 的 dy_pt 不能为 0")
                    entry["dy_pt"] = float(entry.get("dy_pt", 0.0)) + dy
                    records.append({"op": op, "instance_id": change.instance_id, "dy_pt": dy})
                else:
                    if abs(change.dx_pt) > 1e-9 or abs(change.dy_pt) > 1e-9:
                        raise ToolError(
                            "ACTION_NOT_ALLOWED",
                            "resize_component 不接受 dx_pt/dy_pt；位移请用 "
                            "move_component（避免「用 dy_pt 当高度增量」的歧义）。",
                        )
                    if change.width_pt is None and change.height_pt is None \
                            and change.height_delta_pt is None:
                        raise ToolError(
                            "CONTENT_INVALID",
                            "resize_component 需要 width_pt / height_pt / "
                            "height_delta_pt 至少一个。",
                        )
                    if change.height_pt is not None and change.height_delta_pt is not None:
                        raise ToolError(
                            "BOUNDS_VIOLATION",
                            "height_pt 与 height_delta_pt 不能同时给（绝对/增量语义冲突）。",
                        )
                    record: dict[str, Any] = {"op": op, "instance_id": change.instance_id}
                    if change.width_pt is not None:
                        width = float(change.width_pt)
                        if not (MIN_BODY_W_PT <= width <= self.profile.body_width_pt):
                            raise ToolError(
                                "BOUNDS_VIOLATION",
                                f"width_pt={width} 超出 [{MIN_BODY_W_PT}, {self.profile.body_width_pt}]"
                                "（模板正文列宽为上限）。",
                            )
                        entry["width_pt"] = width
                        record["width_pt"] = width
                    if change.height_pt is not None:
                        height = float(change.height_pt)
                        if not (0.0 < height <= MAX_BODY_H_PT):
                            raise ToolError(
                                "BOUNDS_VIOLATION",
                                f"height_pt={height} 超出 (0, {MAX_BODY_H_PT}]。",
                            )
                        entry["height_pt"] = height
                        record["height_pt"] = height
                    if change.height_delta_pt is not None:
                        delta = float(change.height_delta_pt)
                        if not (0.0 <= delta <= 200.0):
                            raise ToolError(
                                "BOUNDS_VIOLATION",
                                f"height_delta_pt={delta} 超出 [0, 200]",
                            )
                        entry["height_delta_pt"] = (
                            float(entry.get("height_delta_pt", 0.0)) + delta
                        )
                        record["height_delta_pt"] = delta
                    records.append(record)
            else:
                raise ToolError("ACTION_UNKNOWN", f"不支持的动作: {op}")
        return new, records

    @staticmethod
    def _section(content: dict[str, Any], key: str) -> dict[str, Any]:
        keys = [str(s.get("key")) for s in content.get("sections", [])]
        if not key or key not in keys:
            raise ToolError(
                "TARGET_NOT_FOUND",
                f"栏目不存在: {key!r}。当前栏目: {keys}",
                suggestion="insert_entry/remove_section/update_section_title/"
                           "set_entry_gap 用 section_key；"
                           "目标既有条目时直接用 instance_id（形如 <栏目key>#<条目id>）。",
            )
        return next(s for s in content["sections"] if s["key"] == key)

    @staticmethod
    def _entry_ref(value: str) -> str:
        """实例引用归一化：`section#entry` / `entry` 都接受（before/after 亦然）。"""
        text = str(value or "")
        return text.split("#", 1)[1] if "#" in text else text

    @classmethod
    def _index_of(cls, section: dict[str, Any], entry_id: str) -> int:
        if not entry_id:
            return len(section["entries"]) - 1
        wanted = cls._entry_ref(entry_id)
        for i, entry in enumerate(section["entries"]):
            if entry["id"] == wanted:
                return i
        raise ToolError("TARGET_NOT_FOUND", f"条目不存在: {entry_id}")

    def _locate(
        self, content: dict[str, Any], change: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        instance_id = str(getattr(change, "instance_id", "") or "")
        section_key = str(getattr(change, "section_key", "") or "")
        entry_id = str(getattr(change, "entry_id", "") or "")
        if instance_id and "#" in instance_id:
            section_key, entry_id = instance_id.split("#", 1)
        elif instance_id and not section_key:
            section_key, entry_id = instance_id, entry_id
        section_key = section_key.split("#", 1)[0]
        sec = self._section(content, section_key)
        entry = sec["entries"][self._index_of(sec, entry_id)]
        return sec, entry

    # ---------- 内部：几何动作落地 ----------

    def _geometry_adjust(self, content: dict[str, Any]) -> dict[str, dict[str, float]]:
        """把内容里的几何覆盖整理成 layout 的 `entry_adjust`（仅 reflow 用）。

        reflow 语义下，几何约束必须**参与完整 flow 排版**：下移/改高后，其后的
        条目、后续栏目与分页一起重排（P2-R2 修复——旧实现只在布局结果上平移
        本栏条目，后续栏目不动，留下交叠）。
        """
        adjust: dict[str, dict[str, float]] = {}
        for sec in content.get("sections", []):
            for entry in sec.get("entries", []):
                per: dict[str, float] = {}
                if entry.get("dy_pt"):
                    per["dy_pt"] = float(entry["dy_pt"])
                if entry.get("height_pt") is not None:
                    per["height_pt"] = float(entry["height_pt"])
                elif entry.get("height_delta_pt"):
                    per["height_delta_pt"] = float(entry["height_delta_pt"])
                if per:
                    adjust[str(entry["id"])] = per
        return adjust

    def _apply_local_geometry(
        self, plan: Any, content: dict[str, Any], RL: Any
    ) -> Any:
        """`local` 模式：只动目标条目，其它组件保持规划位置。

        目标与相邻条目/栏目碰撞、或越出页容量 → 整批失败（不偷偷推开别人）。
        """
        import copy

        plan = copy.deepcopy(plan)
        for sec_content in content.get("sections", []):
            sec_plan = next(
                (s for s in plan.sections if s.section_id == sec_content["key"]), None
            )
            if sec_plan is None:
                continue
            for entry_content in sec_content.get("entries", []):
                dy = float(entry_content.get("dy_pt", 0.0) or 0.0)
                delta_h = float(entry_content.get("height_delta_pt", 0.0) or 0.0)
                height_abs = entry_content.get("height_pt")
                if not dy and not delta_h and height_abs is None:
                    continue
                plan_entry = next(
                    (e for e in sec_plan.entries if e.instance_id == entry_content["id"]),
                    None,
                )
                if plan_entry is None:
                    raise ToolError("TARGET_NOT_FOUND", f"条目不存在: {entry_content['id']}")
                offset = getattr(plan_entry, "text_top_offset_pt", None)
                min_h = (self.geometry.text_top_pt if offset is None else offset) + plan_entry.text_height_pt + 0.5
                if height_abs is not None:
                    target_h = float(height_abs)
                    if target_h < min_h:
                        raise ToolError(
                            "BOUNDS_VIOLATION",
                            f"height_pt={target_h} 小于文字实际需要的高度 "
                            f"{min_h:.1f}pt（会裁切文字）",
                        )
                    delta_h = target_h - plan_entry.body_h_pt
                if delta_h:
                    new_h = plan_entry.body_h_pt + delta_h
                    if new_h < min_h:
                        raise ToolError(
                            "BOUNDS_VIOLATION",
                            f"{plan_entry.instance_id} 调整后高度 {new_h:.1f}pt "
                            f"小于文字高度 {min_h:.1f}pt（会裁切文字）",
                        )
                    plan_entry.body_h_pt = new_h
                if dy:
                    plan_entry.anchor_y_pt += dy
                if entry_content.get("id") == sec_content["entries"][0]["id"] and dy:
                    sec_plan.anchor_y_pt += dy
        self._validate_plan_geometry(plan, RL)
        return plan

    def _validate_plan_geometry(self, plan: Any, RL: Any) -> None:
        """终检：条目不越界、同栏相邻文字不相撞、**跨栏目**文字不相撞。

        跨页时页内坐标重新起算（同一栏目可跨页）。
        """
        for sec_plan in plan.sections:
            prev_text_bottom: float | None = None
            prev_page: int | None = None
            for entry in sec_plan.entries:
                if entry.page_index != prev_page:
                    prev_text_bottom = None
                    prev_page = entry.page_index
                if entry.anchor_y_pt < 0:
                    raise ToolError(
                        "BOUNDS_VIOLATION",
                        f"{entry.instance_id} 被移动到页面上边界之外 "
                        f"(anchor_y={entry.anchor_y_pt:.1f})",
                    )
                offset = getattr(entry, "text_top_offset_pt", None)
                text_top = entry.anchor_y_pt + (self.geometry.text_top_pt if offset is None else offset)
                if (
                    prev_text_bottom is not None
                    and text_top - prev_text_bottom < RL.MIN_ENTRY_GAP_PT
                ):
                    raise ToolError(
                        "COLLISION",
                        f"{entry.instance_id} 与上一条文字重叠 "
                        f"(间距 {text_top - prev_text_bottom:.1f}pt < "
                        f"{RL.MIN_ENTRY_GAP_PT}pt)",
                    )
                bottom = entry.anchor_y_pt + entry.body_h_pt
                if bottom > self.geometry.page_bottom_pt + 1e-6:
                    raise ToolError(
                        "BOUNDS_VIOLATION",
                        f"{entry.instance_id} 移动后越界：底 {bottom:.1f}pt > "
                        f"{self.geometry.page_bottom_pt}pt",
                    )
                prev_text_bottom = text_top + entry.text_height_pt
        try:
            RL.check_section_flow(plan.sections, geometry=self.geometry)
        except RL.LayoutUnsatisfiable as exc:
            raise ToolError("COLLISION", str(exc)) from None

    # ---------- 内部：实例模型与预览 ----------

    def _instance_model(self, plan: Any, content: dict[str, Any]) -> list[dict[str, Any]]:
        protos = {s["key"]: s.get("prototype", "experience_v1")
                  for s in content.get("sections", [])}
        model = []
        for sec in plan.sections:
            section_content = next(s for s in content["sections"] if s["key"] == sec.section_id)
            widths = {e["id"]: e.get("width_pt") for e in section_content["entries"]}
            body_width = 493.75 if self.template_id == "t001" and sec.section_id == "summary" else self.profile.body_width_pt
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
                            "x_pt": self.profile.body_x_pt, "y_pt": round(e.anchor_y_pt, 2),
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
        delivered = sorted({
            int(Path(ref).stem.split("-")[1])
            for ref in refs
            if any(img for img in images)
        })
        if not images:
            delivered = []
        record_path = self.store.revision_dir(artifact_id, revision) / "revision.json"
        record = self.store.load_revision(artifact_id, revision)
        visual = record.setdefault("visual", {"status": "pending", "delivered_pages": []})
        merged = sorted(set(visual.get("delivered_pages", [])) | set(delivered))
        visual["delivered_pages"] = merged
        pages = int(record.get("page_count") or 0)
        if merged and len(merged) >= pages:
            visual["status"] = "delivered"
        elif merged:
            visual["status"] = "partial"
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return record


def format_head_slots(slots: list[str], pattern: str) -> str:
    """按原件的列位模式拼接条目标题槽位（不猜测字体度量）。

    pattern 是原型标题段的**原件原文**（date/org/role 以 2+ 空白分列）。
    替换文本后按显示宽度差调整间隔长度（下限 2 个空白），保持列位大体对齐；
    缺失槽位不写空白字符。这样新条目沿用模板自己的对齐方式，不伪造字宽。
    """
    import re

    parts = [str(s or "").strip() for s in slots]
    if not any(parts):
        return ""
    pieces = re.split(r"([ \u3000]{2,})", str(pattern or "").strip())
    orig = pieces[0::2]
    seps = pieces[1::2]
    out = parts[0]
    for i in (1, 2):
        if not parts[i]:
            continue
        base = ResumeEditService._display_width(orig[i - 1]) if i - 1 < len(orig) else 0
        sep = len(seps[i - 1]) if i - 1 < len(seps) else 12
        gap = sep - max(0, ResumeEditService._display_width(parts[i - 1]) - base)
        out += " " * max(2, gap) + parts[i]
    return out
