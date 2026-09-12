"""简历领域 Service：模板预处理 + 生成 + 受限修复（实施计划 §5.3/§7.2）。

ResumeService 通过 manifest argv 契约调用 fill_resume.py（subprocess backend，
避免 import 用户已修改的脚本内部实现）；模板 hash 由 prepare 在 Python 侧
校验。所有动作在候选副本执行，失败自动丢弃（不注册 artifact）。

字段、照片与组件动作必须来自模板 manifest 白名单；move_rows/resize_rows 由
后端换算为安全 pt 并交给 fill_resume 的受限动作（其内部检查页面边界与重叠）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.contracts.resume import (
    ResumeChange,
    ResumeChangeRequest,
    ResumeGenerateRequest,
)
from skill_toolbox.materials import MaterialCatalog
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.tools.workspace import atomic_write_json, sha256_file
from skill_toolbox.tools.resume_edit import ResumeEditService, format_head_slots  # noqa: F401
from skill_toolbox.unicode_utils import redact_secrets

# 行高换算（fill_resume 估算用 LINE_FACTOR=1.3 × 默认 11pt ≈ 14.3pt；这里取
# 保守整数值，动作层仍有页面边界/重叠机械门兜底）。
ROW_HEIGHT_PT = 14.0
MIN_HEIGHT_PT = 30.0

QA_DIR = "work/qa"
PENDING_VISUAL = "work/qa/pending-visual-review.json"


class ResumeService:
    def __init__(
        self,
        workspace: Path,
        templates_root: Path,
        catalog: MaterialCatalog | None = None,
        runner: ProcessRunner | None = None,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self.workspace = workspace
        self.templates_root = templates_root
        self.catalog = catalog
        self.runner = runner or ProcessRunner()
        self.capabilities = capabilities or {}
        self.vision = bool(self.capabilities.get("vision", False))

    # ---------------- 模板预处理（resume_prepare） ----------------

    def prepare(self, template_id: str) -> OperationResult:
        """校验模板 hash，返回字段/组件 schema、容量、白名单动作与缓存状态。"""
        tpl_dir = self._template_dir(template_id)
        manifest_path = tpl_dir / "manifest.json"
        template = tpl_dir / "template.docx"
        if not manifest_path.is_file() or not template.is_file():
            raise ToolError(
                "TEMPLATE_MISSING",
                f"模板 {template_id} 未入库（缺 manifest.json/template.docx）。"
                "请在 templates/ 下列出的模板里另选一个，或 ask_user_questions "
                "让用户重新选择。",
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("template_sha256")
        if not expected:
            raise ToolError(
                "TEMPLATE_NOT_INDEXED",
                f"模板 {template_id} 未建立 hash 索引，禁止使用。请先完成模板索引。",
            )
        actual = sha256_file(template)
        if actual != expected:
            raise ToolError(
                "TEMPLATE_HASH_MISMATCH",
                f"模板 {template_id} hash 不匹配（期望 {expected[:16]}…，实际 "
                f"{actual[:16]}…）。模板文件已被修改，请重新索引后再操作。",
            )

        fields = []
        for field in manifest.get("fields", []):
            fields.append(
                {
                    "id": field.get("id"),
                    "key": field.get("key"),
                    "mode": field.get("mode", "line"),
                    "template_text": field.get("text", ""),
                    "photo": field.get("mode") == "photo",
                }
            )
        components = []
        for comp in manifest.get("components", []):
            components.append(
                {
                    "id": comp.get("id"),
                    "bbox_pt": comp.get("bbox_pt"),
                    "allowed_actions": comp.get("allowed_actions", []),
                    "field_ids": comp.get("field_ids", []),
                    "capacity_lines": _capacity_lines(comp),
                }
            )
        return OperationResult(
            ok=True,
            status="validated",
            data={
                "template_id": template_id,
                "template_hash": expected[:16],
                "fields": fields,
                "components": components,
                "photo_supported": any(f.get("mode") == "photo" for f in manifest.get("fields", [])),
                "target_actions": [
                    "resume_generate",
                    "resume_repair",
                ],
                "cached": True,
            },
        )

    # ---------------- 生成（resume_generate） ----------------

    def generate(self, request: ResumeGenerateRequest) -> OperationResult:
        """生成 resume data、填充候选副本、处理照片、自动溢出估算与机械检查。

        失败（fill rc!=0 或 overflow_risks 非空）不注册 artifact；成功写
        QAReport（mechanical=passed，visual 按能力）。"""
        tpl_dir = self._template_dir(request.template_id)
        manifest = json.loads(
            (tpl_dir / "manifest.json").read_text(encoding="utf-8")
        )
        expected = manifest.get("template_sha256")
        if not expected or sha256_file(tpl_dir / "template.docx") != expected:
            raise ToolError(
                "TEMPLATE_HASH_MISMATCH",
                f"模板 {request.template_id} hash 校验失败，拒绝填充。",
            )
        # 字段白名单：manifest 字段 key + photo。target_role 是上层上下文，
        # 当前模板没有统一字段锚点，不能静默写入未声明文本框。
        allowed_keys = {
            f["key"] for f in manifest.get("fields", [])
        } | {f["id"] for f in manifest.get("fields", [])} | {"photo"}
        unknown_fields = sorted(set(request.fields) - allowed_keys)
        if unknown_fields:
            raise ToolError(
                "FIELD_UNSUPPORTED",
                f"模板 {request.template_id} 不支持的字段: {unknown_fields}。"
                f"只填 manifest 字段（{sorted(allowed_keys)[:8]}…）。",
            )

        data_path = self.workspace / "work" / "resume_data.json"
        data = {"fields": dict(request.fields)}
        if request.target_role:
            # 只有模板明确声明求职意向字段时才注入目标岗位，避免把自由文本
            # 误当作可编辑组件；没有该字段时保持请求可用但不伪造替换记录。
            role_keys = {
                f["key"] for f in manifest.get("fields", [])
                if str(f.get("key", "")).lower() in {"intent", "target_role", "position"}
            }
            if role_keys:
                data["fields"][next(iter(role_keys))] = request.target_role
        if request.photo_asset_id:
            photo_path = self._asset_path(request.photo_asset_id)
            if photo_path is None:
                raise ToolError(
                    "ASSET_UNKNOWN",
                    f"photo_asset_id 不存在: {request.photo_asset_id}。"
                    "请从 document.json 的 assets[].id 复制真实 ID。",
                )
            data["fields"]["photo"] = photo_path
        data_path.parent.mkdir(parents=True, exist_ok=True)
        temp = data_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(data_path)

        out = self.workspace / "artifacts" / "resume.docx"
        out.parent.mkdir(parents=True, exist_ok=True)
        report = self._run_fill(tpl_dir, data_path, out)
        if report.get("ok") is not True:
            raise ToolError(
                "FILL_FAILED",
                str(report.get("error") or "fill_resume 失败"),
                retryable=True,
                suggestion="检查 resume_data.json 字段是否合法（字段名、文本编码）。",
            )
        overflow = report.get("overflow_risks") or []
        if overflow:
            hint = overflow[0].get("hint", "请精简字段内容后重试")
            raise ToolError(
                "FILL_OVERFLOW",
                f"内容溢出（{len(overflow)} 个字段）: {hint}",
                retryable=True,
                suggestion="按 hint 精简对应字段内容，重新调用 resume_generate。",
            )
        # 机械门通过：写 QAReport。视觉检查必须有真实证据，不能由 capability
        # 猜测；当前 Service 没有视觉复核实现，因此明确登记待办。
        self._write_qa(mechanical="passed", template_id=request.template_id)
        self._write_pending_visual("resume", out)
        return OperationResult(
            ok=True,
            status="validated",
            artifact_id=f"resume-{_short_hash(report.get('template', ''))}",
            data={
                "path": "artifacts/resume.docx",
                "replaced": report.get("replaced", []),
                "warnings": report.get("warnings", []),
                "overflow_risks": [],
                "template": request.template_id,
            },
            next_action="交付（finish_task artifacts=artifacts/resume.docx）或 "
                        "resume_repair 修复版面。",
        )

    # ---------------- 受限修复（resume_repair） ----------------

    def repair(self, request: ResumeChangeRequest) -> OperationResult:
        """changes[] 只允许白名单动作；候选副本失败自动回滚（不注册产物）。"""
        if not request.changes:
            raise ToolError("REPAIR_EMPTY", "changes 不能为空")
        tpl_dir, manifest, report = self._load_last_report(request.artifact_id)
        actions = []
        for change in request.changes:
            actions.extend(self._change_to_actions(change, manifest))
        if not actions:
            raise ToolError(
                "REPAIR_NO_ACTION",
                "changes 未产生任何可执行动作（请检查 component_id 与白名单）。",
            )
        # 候选副本：基于上次 generate 的 fields 重新填充 + 本次 actions。
        # 关键：replace_text/replace_asset 动作组件的字段要从 fields 中移除，
        # 否则该组件文本框已被 fields 替换、不再含模板原文锚点，fill 的
        # replace_text 会报"未找到匹配文本框"（fill 动作按模板原文匹配）。
        data_path = self.workspace / "work" / "resume_data.json"
        if not data_path.is_file():
            raise ToolError(
                "REPAIR_NO_DATA",
                "缺少 work/resume_data.json（先 resume_generate 生成基线）。",
            )
        base = json.loads(data_path.read_text(encoding="utf-8"))
        base_fields = dict(base.get("fields", {}))
        action_components = {
            change.component_id
            for change in request.changes
            if change.action in {"replace_text", "replace_asset"}
        }
        if action_components:
            remove_keys: set[str] = set()
            for comp in manifest.get("components", []):
                if comp.get("id") in action_components:
                    remove_keys.update(comp.get("field_ids", []) or [])
                    remove_keys.update(comp.get("content_slot", []) or [])
            base_fields = {
                key: value
                for key, value in base_fields.items()
                if key not in remove_keys and key != "photo"
            }
        data = {"fields": base_fields, "actions": actions}
        temp_data = self.workspace / "work" / "resume_repair_data.json"
        temp_data.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        out = self.workspace / "artifacts" / "resume.docx"
        report = self._run_fill(tpl_dir, temp_data, out)
        if report.get("ok") is not True:
            # 候选失败自动丢弃：不覆盖基线产物
            raise ToolError(
                "REPAIR_FAILED",
                str(report.get("error") or "repair 填充失败"),
                retryable=True,
                suggestion="检查 changes 的 component_id/action 是否在模板白名单内。",
            )
        overflow = report.get("overflow_risks") or []
        if overflow:
            raise ToolError(
                "REPAIR_OVERFLOW",
                f"repair 后内容溢出: {overflow[0].get('hint', '')}",
                retryable=True,
                suggestion="撤回本次 changes 或进一步精简内容。",
            )
        self._write_qa(mechanical="passed")
        self._write_pending_visual("resume_repair", out)
        return OperationResult(
            ok=True,
            status="validated",
            artifact_id=request.artifact_id,
            data={
                "path": "artifacts/resume.docx",
                "action_records": report.get("actions", []),
                "warnings": report.get("warnings", []),
            },
            next_action="交付（finish_task artifacts=artifacts/resume.docx）。",
        )

    # ---------------- helpers ----------------
    def _template_dir(self, template_id: str) -> Path:
        tpl = (self.templates_root / template_id).resolve()
        try:
            tpl.relative_to(self.templates_root.resolve())
        except ValueError as exc:
            raise ToolError(
                "TEMPLATE_UNKNOWN",
                f"未知模板: {template_id}。可选 {self._available_templates()}",
            ) from exc
        if not tpl.is_dir():
            raise ToolError(
                "TEMPLATE_UNKNOWN",
                f"未知模板: {template_id}。可选 {self._available_templates()}",
            )
        return tpl

    def _available_templates(self) -> list[str]:
        if not self.templates_root.is_dir():
            return []
        return sorted(
            p.name for p in self.templates_root.iterdir()
            if (p / "manifest.json").is_file() and (p / "template.docx").is_file()
        )

    def _asset_path(self, asset_id: str) -> str | None:
        return self.catalog.asset_path(asset_id) if self.catalog else None

    def _run_fill(self, tpl_dir: Path, data_path: Path, out: Path) -> dict[str, Any]:
        fill_script = (
            self.templates_root.parent / "scripts" / "fill_resume.py"
        )
        if not fill_script.is_file():
            raise ToolError(
                "FILL_SCRIPT_MISSING",
                f"fill_resume.py 缺失: {fill_script}",
            )
        try:
            completed = self.runner.run(
                [
                    sys.executable,
                    str(fill_script.resolve()),
                    str(tpl_dir.resolve()),
                    str(data_path.resolve()),
                    str(out.resolve()),
                ],
                timeout=180,
                cwd=self.workspace,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                "FILL_TIMEOUT", f"fill_resume 超时（{exc}）", retryable=True
            ) from None
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode == 0:
            try:
                report = json.loads(stdout)
                report.setdefault("ok", True)
                return report
            except json.JSONDecodeError:
                return {"ok": False, "error": f"fill 输出非 JSON: {redact_secrets(stdout[-500:])}"}
        return {
            "ok": False,
            "error": redact_secrets(stderr.strip() or stdout.strip() or f"rc={completed.returncode}"),
        }

    def _load_last_report(
        self, artifact_id: str
    ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        """取最近一次成功填充的模板目录与 manifest（repair 基线的确定来源）。"""
        tpl_id = self._template_id_from_artifact(artifact_id)
        tpl_dir = self._template_dir(tpl_id)
        manifest = json.loads(
            (tpl_dir / "manifest.json").read_text(encoding="utf-8")
        )
        return tpl_dir, manifest, {}

    def _template_id_from_artifact(self, artifact_id: str) -> str:
        # artifact_id 形如 resume-<hash>；用 work/resume_data.json 记录不可靠，
        # 改为最近一次填充的模板：从 work/qa/resume.json 读 template。
        qa_path = self.workspace / QA_DIR / "resume.json"
        if qa_path.is_file():
            try:
                qa = json.loads(qa_path.read_text(encoding="utf-8"))
                if qa.get("template"):
                    return str(qa["template"])
            except (OSError, ValueError):
                pass
        raise ToolError(
            "REPAIR_NO_BASELINE",
            "无法确定修复基线模板（先 resume_generate 一次）。",
        )

    def _change_to_actions(self, change: ResumeChange, manifest: dict[str, Any]) -> list[dict]:
        components = {c["id"]: c for c in manifest.get("components", [])}
        if change.action == "replace_text":
            if change.component_id not in components:
                raise ToolError(
                    "COMPONENT_UNKNOWN",
                    f"未知组件: {change.component_id}（模板 components 见 resume_prepare）",
                )
            return [
                {
                    "action": "replace_text",
                    "component": change.component_id,
                    "field": change.component_id,
                    "value": change.text,
                }
            ]
        if change.action == "replace_asset":
            if change.component_id not in components:
                raise ToolError(
                    "COMPONENT_UNKNOWN",
                    f"未知组件: {change.component_id}（模板 components 见 resume_prepare）",
                )
            if change.asset_id == "__remove__":
                return [
                    {"action": "replace_asset", "component": change.component_id, "value": "__remove__"}
                ]
            path = self._asset_path(change.asset_id)
            if path is None:
                raise ToolError(
                    "ASSET_UNKNOWN",
                    f"asset_id 不存在: {change.asset_id}",
                )
            return [
                {"action": "replace_asset", "component": change.component_id, "value": path}
            ]
        comp = components.get(change.component_id)
        if comp is None:
            raise ToolError(
                "COMPONENT_UNKNOWN",
                f"未知组件: {change.component_id}（模板 components 见 resume_prepare）",
            )
        if change.action == "shift_components":
            allowed = comp.get("allowed_actions", [])
            if "shift_components" not in allowed:
                raise ToolError(
                    "ACTION_NOT_ALLOWED",
                    f"组件 {change.component_id} 不允许 shift_components（白名单 {allowed}）",
                )
            dy_pt = round(change.move_rows * ROW_HEIGHT_PT, 1)
            return [
                {
                    "action": "shift_components",
                    "component_ids": [change.component_id],
                    "dy_pt": dy_pt,
                }
            ]
        if change.action == "resize_component":
            allowed = comp.get("allowed_actions", [])
            if "resize_component" not in allowed:
                raise ToolError(
                    "ACTION_NOT_ALLOWED",
                    f"组件 {change.component_id} 不允许 resize_component（白名单 {allowed}）",
                )
            # resize 基准：manifest 声明的 min_height_pt（bbox_pt 是 [宽,高]
            # 二元组，且 min/max 是权威范围）；resize_rows 向上扩展并受
            # min/max_height_pt 机械门约束（fill 内部检查）。
            base_height = float(comp.get("min_height_pt") or MIN_HEIGHT_PT)
            height_pt = round(base_height + change.resize_rows * ROW_HEIGHT_PT, 1)
            return [
                {
                    "action": "resize_component",
                    "component": change.component_id,
                    "height_pt": height_pt,
                }
            ]
        raise ToolError(
            "ACTION_UNSUPPORTED",
            f"不支持的修复动作: {change.action}（只允许 "
            "replace_text/replace_asset/shift_components/resize_component）",
        )

    def _write_qa(self, mechanical: str, template_id: str | None = None) -> None:
        qa_dir = self.workspace / QA_DIR
        qa_dir.mkdir(parents=True, exist_ok=True)
        qa = {"mechanical": mechanical, "visual": "not_run"}
        if template_id:
            qa["template"] = template_id
        path = qa_dir / "resume.json"
        atomic_write_json(path, qa)

    def _write_pending_visual(self, step: str, artifact: Path) -> None:
        path = self.workspace / PENDING_VISUAL
        items: list[dict[str, Any]] = []
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = raw.get("items", []) if isinstance(raw, dict) else []
            except (OSError, ValueError):
                items = []
        items.append(
            {
                "step": f"{step}_visual_review",
                "reason": "当前 Service 未执行真实 Vision 视觉复核，需后续检查文字溢出、重叠与版式",
                "affected_artifacts": [str(artifact.relative_to(self.workspace))],
                "review_required": True,
                "status": "skipped",
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def _capacity_lines(comp: dict[str, Any]) -> int | None:
    """组件容量（可容纳行数）粗估：bbox 高 / 行高。"""
    bbox = comp.get("bbox_pt")
    if not bbox or len(bbox) < 2:
        return None
    try:
        # manifest bbox_pt 规范为 [width, height]；兼容旧四元组时取最后一项。
        height = float(bbox[-1])
    except (TypeError, ValueError):
        return None
    return max(1, int(height / (11.0 * 1.3)))


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
