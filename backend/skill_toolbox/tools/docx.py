"""DOCX 领域 Service：草稿 + 顺序 blocks + 生成/检查/渲染编排（实施计划 §5.4）。

模型只通过 docx_start/docx_add_blocks/docx_finalize/docx_repair 操作；图片只
传 asset_id（后端解析真实路径）；build/inject_toc/postcheck/render 编排为
后端操作，不再对 LLM 暴露 exec_cmd/spec_append。底层 docx_pro 脚本保留为
兼容实现，由本 Service 以 subprocess 调用（计划 §目标 9）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.contracts.docx import (
    DocxAddBlocksRequest,
    DocxBlock,
    DocxFinalizeRequest,
    DocxRepairRequest,
    DocxStartRequest,
)
from skill_toolbox.materials import MaterialCatalog
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.unicode_utils import redact_secrets

DRAFT_DIR = "work/drafts"
QA_DIR = "work/qa"
PENDING_VISUAL = "work/qa/pending-visual-review.json"
# 目录注入阈值（对齐 docx_pro prompt：standard/academic 且 H1 ≥ 3）
TOC_MIN_HEADINGS = 3
TOC_COMPLEXITIES = {"standard", "academic"}


class DocxService:
    def __init__(
        self,
        workspace: Path,
        scripts_root: Path,
        catalog: MaterialCatalog | None = None,
        runner: ProcessRunner | None = None,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self.workspace = workspace
        self.scripts_root = scripts_root
        self.catalog = catalog
        self.runner = runner or ProcessRunner()
        self.capabilities = capabilities or {}
        self.vision = bool(self.capabilities.get("vision", False))

    # ---------------- docx_start ----------------

    def start(self, request: DocxStartRequest) -> OperationResult:
        document_id = f"docx-{_short_hash(request.title)}"
        draft = {
            "document_id": document_id,
            "title": request.title,
            "complexity": request.complexity,
            "scene": request.scene,
            "author": request.author,
            "date": request.date,
            "blocks": [],
        }
        path = self._draft_path(document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(path, draft)
        return OperationResult(
            ok=True,
            status="created",
            artifact_id=document_id,
            data={
                "document_id": document_id,
                "complexity": request.complexity,
                "block_count": 0,
                "draft": self._rel(path),
            },
            next_action="docx_add_blocks 追加内容（每次 ≤8 个有序 blocks）。",
        )

    # ---------------- docx_add_blocks ----------------

    def add_blocks(self, request: DocxAddBlocksRequest) -> OperationResult:
        draft = self._load_draft(request.document_id)
        if not request.blocks:
            raise ToolError("DOCX_EMPTY_BLOCKS", "blocks 不能为空数组")
        errors: list[ToolError] = []
        for index, block in enumerate(request.blocks):
            try:
                self._validate_block(block)
            except ToolError as exc:
                errors.append(exc)
        if errors:
            first = errors[0]
            # 单错误保留原始稳定 code（如 DOCX_ASSET_UNKNOWN），多错误聚合
            raise ToolError(
                first.code if len(errors) == 1 else "DOCX_BLOCK_INVALID",
                "；".join(f"blocks[{i}]: {exc}" for i, exc in enumerate(errors)),
                retryable=True,
                suggestion="修正对应 block 后重试（图片只传 asset_id）。",
            )
        spec_blocks = [self._to_spec_block(block) for block in request.blocks]
        draft["blocks"].extend(spec_blocks)
        self._atomic_json(self._draft_path(request.document_id), draft)
        return OperationResult(
            ok=True,
            status="created",
            artifact_id=request.document_id,
            data={
                "document_id": request.document_id,
                "total_blocks": len(draft["blocks"]),
                "appended": len(spec_blocks),
            },
            next_action="继续 docx_add_blocks 或 docx_finalize。",
        )

    def _validate_block(self, block: DocxBlock) -> None:
        if block.type == "heading":
            if not block.text.strip():
                raise ToolError("DOCX_BLOCK_INVALID", "heading 缺少 text")
        elif block.type == "paragraph":
            if not block.text.strip():
                raise ToolError("DOCX_BLOCK_INVALID", "paragraph 缺少 text")
        elif block.type == "image":
            if not block.asset_id:
                raise ToolError(
                    "DOCX_IMAGE_NO_ASSET",
                    "image block 必须传 asset_id（从 document.json 的 assets[].id 复制），"
                    "不接受任意路径。",
                )
            if self._asset_path(block.asset_id) is None:
                raise ToolError(
                    "DOCX_ASSET_UNKNOWN",
                    f"asset_id 不存在: {block.asset_id}。请从 document.json 复制真实 ID。",
                )
        elif block.type == "table":
            if not block.headers or not block.rows:
                raise ToolError("DOCX_BLOCK_INVALID", "table 必须提供 headers 与 rows")
        elif block.type == "formula":
            if not block.latex.strip():
                raise ToolError("DOCX_BLOCK_INVALID", "formula 缺少 latex")

    def _to_spec_block(self, block: DocxBlock) -> dict[str, Any]:
        if block.type == "image":
            path = self._asset_path(block.asset_id)
            spec: dict[str, Any] = {
                "type": "image",
                "source": path or "",
                "caption": block.caption,
            }
            return spec
        if block.type == "table":
            return {
                "type": "table",
                "caption": block.caption,
                "headers": block.headers,
                "rows": block.rows,
            }
        if block.type == "formula":
            return {"type": "formula", "latex": block.latex, "caption": block.caption}
        if block.type == "heading":
            return {"type": "heading", "text": block.text, "level": block.level}
        return {"type": "paragraph", "text": block.text}

    # ---------------- docx_finalize ----------------

    def finalize(self, request: DocxFinalizeRequest) -> OperationResult:
        draft = self._load_draft(request.document_id)
        output_name = request.output_name or f"{_safe_name(draft['title']) or 'document'}.docx"
        spec_path = self.workspace / "work" / "spec.json"
        spec = {
            "title": draft["title"],
            "complexity": draft["complexity"],
            "blocks": draft["blocks"],
        }
        if draft.get("scene"):
            spec["scene"] = draft["scene"]
        if draft.get("author"):
            spec["author"] = draft["author"]
        if draft.get("date"):
            spec["date"] = draft["date"]
        self._atomic_json(spec_path, spec)

        out = self.workspace / "artifacts" / output_name
        out.parent.mkdir(parents=True, exist_ok=True)

        # build → (可选) inject_toc → postcheck 机械门
        self._run_script("build_docx.py", [str(spec_path), str(out)], "DOCX_BUILD_FAILED")
        heading_count = sum(
            1 for block in draft["blocks"]
            if block.get("type") == "heading" and block.get("level") == 1
        )
        if (
            draft["complexity"] in TOC_COMPLEXITIES
            and heading_count >= TOC_MIN_HEADINGS
        ):
            tmp_toc = self.workspace / "work" / "_toc.docx"
            self._run_script(
                "inject_toc.py", [str(out), str(tmp_toc)], "DOCX_TOC_FAILED"
            )
            tmp_toc.replace(out)

        check_report = self._run_script(
            "postcheck_docx.py", [str(out), "--json"], "DOCX_CHECK_FAILED"
        )
        if check_report.get("ok") is not True:
            issues = check_report.get("issues", [])
            raise ToolError(
                "DOCX_CHECK_FAILED",
                f"机械检查未通过（{len(issues)} 项）: {issues[:3]}",
                retryable=True,
                suggestion="docx_repair 只修复被点名 issue；请按 issues 修正对应 block。",
            )

        self._write_qa(mechanical="passed")
        self._write_pending_visual("docx", out)
        return OperationResult(
            ok=True,
            status="validated",
            artifact_id=request.document_id,
            data={
                "path": self._rel(out),
                "block_count": len(draft["blocks"]),
                "check": check_report,
                "visual": "not_run",
            },
            next_action="交付（finish_task artifacts=<输出 docx>）。",
        )

    # ---------------- docx_repair ----------------

    def repair(self, request: DocxRepairRequest) -> OperationResult:
        """只修复质量报告点名的问题：fix 映射为草稿 block 的结构化修改。"""
        draft = self._load_draft(request.artifact_id)
        fix = request.fix or {}
        changed = False
        fix_type = str(fix.get("type", ""))
        if fix_type == "replace_block_text":
            block_index = int(fix.get("block_index", -1))
            if not (0 <= block_index < len(draft["blocks"])):
                raise ToolError("DOCX_REPAIR_INDEX", "block_index 越界")
            draft["blocks"][block_index]["text"] = str(fix.get("text", ""))
            changed = True
        elif fix_type == "remove_block":
            block_index = int(fix.get("block_index", -1))
            if not (0 <= block_index < len(draft["blocks"])):
                raise ToolError("DOCX_REPAIR_INDEX", "block_index 越界")
            del draft["blocks"][block_index]
            changed = True
        else:
            raise ToolError(
                "DOCX_REPAIR_UNSUPPORTED",
                f"不支持的修复类型: {fix_type}（只允许 "
                "replace_block_text / remove_block）",
            )
        if changed:
            self._atomic_json(self._draft_path(request.artifact_id), draft)
        return self.finalize(
            DocxFinalizeRequest(
                document_id=request.artifact_id,
                output_name=fix.get("output_name", ""),
            )
        )

    # ---------------- helpers ----------------

    def _draft_path(self, document_id: str) -> Path:
        return self.workspace / DRAFT_DIR / f"{document_id}.json"

    def _load_draft(self, document_id: str) -> dict[str, Any]:
        path = self._draft_path(document_id)
        if not path.is_file():
            raise ToolError(
                "DOCX_NO_DRAFT",
                f"草稿不存在: {document_id}。先 docx_start 创建草稿。",
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError("DOCX_DRAFT_CORRUPT", f"草稿损坏: {exc}") from None

    def _asset_path(self, asset_id: str) -> str | None:
        if self.catalog is None:
            return None
        for ir in self.catalog.irs.values():
            asset = ir.asset_by_id(asset_id)
            if asset is not None:
                return asset.path
        return None

    def _run_script(
        self, script_name: str, args: list[str], fail_code: str
    ) -> dict[str, Any]:
        script = self.scripts_root / script_name
        if not script.is_file():
            raise ToolError(fail_code, f"脚本缺失: {script}")
        try:
            completed = self.runner.run(
                [sys.executable, str(script.resolve()), *args],
                timeout=300,
                cwd=self.workspace,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(fail_code, f"{script_name} 超时（{exc}）", retryable=True) from None
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise ToolError(
                fail_code,
                redact_secrets(stderr.strip() or stdout.strip() or f"{script_name} rc={completed.returncode}"),
                retryable=True,
            )
        if script_name == "postcheck_docx.py":
            # --json 模式输出 checks 数组：每项 {name, passed, severity, details}
            try:
                checks = json.loads(stdout)
            except json.JSONDecodeError:
                raise ToolError(
                    fail_code, f"postcheck 输出非 JSON: {redact_secrets(stdout[-500:])}"
                ) from None
            errors = [
                c for c in checks
                if isinstance(c, dict) and not c.get("passed") and c.get("severity") == "error"
            ]
            warnings = [
                c for c in checks
                if isinstance(c, dict) and not c.get("passed") and c.get("severity") == "warning"
            ]
            if errors:
                raise ToolError(
                    fail_code,
                    f"机械检查未通过（{len(errors)} 个 error，{len(warnings)} 个 warning）: "
                    + "; ".join(
                        f"{c.get('name')}: {c.get('details', '')}" for c in errors[:3]
                    ),
                    retryable=True,
                    suggestion="docx_repair 只修复被点名 issue；请按上述检查项修正对应 block。",
                )
            return {"ok": True, "errors": len(errors), "warnings": len(warnings), "checks": checks}
        return {"ok": True}

    def _write_qa(self, mechanical: str) -> None:
        qa_dir = self.workspace / QA_DIR
        qa_dir.mkdir(parents=True, exist_ok=True)
        qa = {"mechanical": mechanical, "visual": "not_run"}
        path = qa_dir / "docx.json"
        self._atomic_json(path, qa)

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
                "reason": "当前 Service 未执行真实 Vision 视觉复核，需后续检查文字/图片重叠与版式",
                "affected_artifacts": [str(artifact.relative_to(self.workspace))],
                "review_required": True,
                "status": "skipped",
            }
        )
        self._atomic_json(path, {"items": items})

    def _atomic_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _rel(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.workspace.resolve()))


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _safe_name(value: str) -> str:
    import re

    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("._")
    return cleaned or "document"
