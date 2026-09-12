"""PPT 领域 Service：vendor 适配 + 有界生成（实施计划 §5.5/§7.5）。

分层：
- V1 生成走 python-pptx 直构（固定布局池），完全确定、无 SVG 自由度；模型
  只传 layout/title/bullets/asset_id 等数据参数。
- vendor（ppt-master）作为外部仓库通过 vendor_fingerprint/契约测试追踪版本；
  pptx_delivery_check 作为可选 vendor 结构门（存在且可调用时启用）。
- 不暴露 project_manager/project_import/自由 SVG authoring（§4.2/§7 禁止）。

无 Vision：图片只按 asset 元数据 high_confidence_only；视觉页面复核跳过并
登记 pending-visual-review（计划 §8），不伪装视觉通过。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.util import Inches, Pt

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.contracts.ppt import (
    DeckOutline,
    PptGenerateRequest,
    PptRepairRequest,
    SlideSpec,
)
from skill_toolbox.materials import MaterialCatalog
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.tools.workspace import atomic_write_json
from skill_toolbox.unicode_utils import redact_secrets

OUTLINE_DIR = "work/outlines"
QA_DIR = "work/qa"
PENDING_VISUAL = "work/qa/pending-visual-review.json"

# 固定布局池（V1 有界能力；模型不传坐标/EMU/SVG）
_LAYOUTS = {
    "title", "section", "bullets", "two_column", "image_text", "image_full",
    "table", "agenda", "quote", "end",
}
_SLIDE_W = 13.333  # 16:9 英寸
_SLIDE_H = 7.5
_MARGIN = 0.6


class PptService:
    def __init__(
        self,
        workspace: Path,
        vendor_root: Path,
        catalog: MaterialCatalog | None = None,
        runner: ProcessRunner | None = None,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self.workspace = workspace
        self.vendor_root = vendor_root  # 当前发行包中的 ppt-master vendor 快照
        self.catalog = catalog
        self.runner = runner or ProcessRunner()
        self.capabilities = capabilities or {}
        self.vision = bool(self.capabilities.get("vision", False))

    # ---------------- vendor 适配（§4.2/§7） ----------------

    def vendor_fingerprint(self) -> dict[str, Any]:
        """上游 manifest/SKILL 版本 + 脚本存在性（契约变化可被测试发现）。"""
        manifest_path = self.vendor_root / "manifest.json"
        skill_md = self.vendor_root / "SKILL.md"
        version = "unknown"
        if manifest_path.is_file():
            try:
                version = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("metadata", {}).get("version", "")) or version
            except (OSError, ValueError):
                pass
        if skill_md.is_file():
            try:
                text = skill_md.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if "version" in line.lower() and ":" in line:
                        version = line.split(":", 1)[1].strip().strip('"')
                        break
            except OSError:
                pass
        key_scripts = [
            "project_manager.py",
            "svg_to_pptx.py",
            "svg_quality_checker.py",
            "template_fill_pptx.py",
            "pptx_delivery_check.py",
        ]
        return {
            "version": version,
            "scripts_present": {
                name: (self.vendor_root / "scripts" / name).is_file()
                for name in key_scripts
            },
            "adapter": "skill_toolbox/tools/ppt.py",
            "generation_backend": "python-pptx-fixed-layout",
            "vendor_generation_adapter": "not_connected",
        }

    def delivery_check(self, pptx_path: Path) -> dict[str, Any]:
        """可选 vendor 结构门：pptx_delivery_check.py（存在才启用）。

        结构门失败不会伪造通过：机械结构问题（坏包/缺图片 part）直接拒绝。
        """
        script = self.vendor_root / "scripts" / "pptx_delivery_check.py"
        if not script.is_file():
            return {"ok": True, "vendor_check": "not_available"}
        try:
            completed = self.runner.run(
                [sys.executable, str(script.resolve()), str(pptx_path)],
                timeout=180,
                cwd=self.workspace,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("PPT_VENDOR_CHECK_TIMEOUT", f"pptx_delivery_check 超时（{exc}）", retryable=True) from None
        stdout = completed.stdout.decode("utf-8", errors="replace")
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            # 非 JSON 输出：vendor 脚本契约变化 → 按结构门失败处理（不静默放行）
            raise ToolError(
                "PPT_VENDOR_CHECK_INVALID",
                f"pptx_delivery_check 输出非 JSON（上游契约可能变化）: {redact_secrets(stdout[-400:])}",
            ) from None
        return {"ok": report.get("ok", True), "vendor_check": report}

    # ---------------- ppt_create_outline ----------------

    def create_outline(
        self,
        topic: str,
        audience: str = "",
        page_count: int = 8,
        style: str = "clean",
        selected_source_ids: list[str] | None = None,
    ) -> OperationResult:
        """生成有界页面大纲（固定布局池）+ 注册 outline 文件。"""
        if not topic.strip():
            raise ToolError("PPT_NO_TOPIC", "topic 不能为空")
        if not 1 <= page_count <= 30:
            raise ToolError("PPT_PAGE_COUNT", "page_count 必须在 1-30")
        slides = self._plan_pages(topic, audience, page_count, style)
        outline = DeckOutline(
            topic=topic,
            audience=audience,
            page_count=page_count,
            style=style,
            selected_source_ids=selected_source_ids or [],
            slides=slides,
        )
        outline_id = f"outline-{_short_hash(topic)}"
        path = self.workspace / OUTLINE_DIR / f"{outline_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, outline.model_dump())
        return OperationResult(
            ok=True,
            status="created",
            artifact_id=outline_id,
            data={
                "outline_id": outline_id,
                "page_count": page_count,
                "style": style,
                "slides": [s.model_dump() for s in slides],
            },
            next_action="ppt_generate 用 slides 参数生成 PPTX。",
        )

    def _plan_pages(
        self, topic: str, audience: str, page_count: int, style: str
    ) -> list[SlideSpec]:
        slides: list[SlideSpec] = [SlideSpec(layout="title", title=topic)]
        if page_count > 1:
            slides.append(
                SlideSpec(layout="agenda", title="目录", bullets=[f"{i+1}. {topic}" for i in range(min(4, page_count - 1))])
            )
        # title + agenda + body + end = page_count（end 恒存在）
        body_count = max(0, page_count - 3)
        for index in range(body_count):
            slides.append(
                SlideSpec(
                    layout="bullets" if index % 2 == 0 else "two_column",
                    title=f"{topic} · {index + 1}",
                    bullets=["要点一", "要点二", "要点三"],
                    columns=["左栏要点", "右栏要点"],
                )
            )
        if page_count > 1:
            slides.append(SlideSpec(layout="end", title="谢谢"))
        return slides[:page_count]

    # ---------------- ppt_generate ----------------

    def generate(self, request: PptGenerateRequest) -> OperationResult:
        """python-pptx 直构（固定布局）；结构门失败不会交付。"""
        if not request.slides:
            raise ToolError("PPT_NO_SLIDES", "slides 不能为空（先 ppt_create_outline）")
        if len(request.slides) > 30:
            raise ToolError("PPT_SLIDE_LIMIT", "slides 最多 30 页")
        for index, slide in enumerate(request.slides):
            if slide.layout not in _LAYOUTS:
                raise ToolError(
                    "PPT_LAYOUT_UNSUPPORTED",
                    f"slides[{index}].layout='{slide.layout}' 不支持，允许 {sorted(_LAYOUTS)}",
                )
            if slide.asset_id:
                if self.catalog is None or self._asset_path(slide.asset_id) is None:
                    raise ToolError(
                        "PPT_ASSET_UNKNOWN",
                        f"slides[{index}] asset_id 不存在: {slide.asset_id}。"
                        "请从 document.json 的 assets[].id 复制真实 ID。",
                    )

        output = self.workspace / "artifacts" / f"{_safe_name(request.outline_id or 'deck')}.pptx"
        output.parent.mkdir(parents=True, exist_ok=True)
        prs = Presentation()
        prs.slide_width = Inches(_SLIDE_W)
        prs.slide_height = Inches(_SLIDE_H)
        for slide_spec in request.slides:
            self._add_slide(prs, slide_spec)
        prs.save(output)

        # 结构门：python-pptx 可打开 + 图片引用存在 + vendor delivery check（可选）
        self._structural_gate(output)

        self._write_qa()
        self._write_pending_visual(output)
        return OperationResult(
            ok=True,
            status="validated",
            artifact_id=request.outline_id,
            data={
                "path": str(output.relative_to(self.workspace)),
                "slide_count": len(request.slides),
                "vendor": self.vendor_fingerprint(),
                "visual": "not_run",
                "pending_visual_review": PENDING_VISUAL,
            },
            next_action="交付（finish_task artifacts=<输出 pptx>）。",
        )

    def _add_slide(self, prs: Presentation, spec: SlideSpec) -> None:
        blank = prs.slide_layouts[6]
        slide = prs.slides.add_slide(blank)
        if spec.layout == "title":
            _add_title(slide, spec.title, 0.9, 2.6, 40)
            if spec.body:
                _add_textbox(slide, spec.body, 0.9, 4.0, 9.0, 0.8, 18)
            return
        if spec.layout == "end":
            _add_title(slide, spec.title or "谢谢", 0.9, 2.6, 40)
            return
        if spec.layout == "section":
            _add_title(slide, spec.title, 0.9, 1.0, 32)
            if spec.body:
                _add_textbox(slide, spec.body, 0.9, 2.2, 11.5, 3.0, 16)
            return
        # 通用页眉
        _add_title(slide, spec.title, _MARGIN, 0.35, 24)
        content_top = 1.3
        if spec.layout == "bullets" or spec.layout == "agenda":
            bullets = spec.bullets or ["（待补充要点）"]
            _add_bullets(slide, bullets, _MARGIN, content_top, _SLIDE_W - 2 * _MARGIN, _SLIDE_H - content_top - _MARGIN)
        elif spec.layout == "two_column":
            left, right = spec.columns or ["左栏要点", "右栏要点"]
            half = (_SLIDE_W - 3 * _MARGIN) / 2
            _add_bullets(slide, left.splitlines(), _MARGIN, content_top, half, _SLIDE_H - content_top - _MARGIN)
            _add_bullets(slide, right.splitlines(), _MARGIN + half + _MARGIN, content_top, half, _SLIDE_H - content_top - _MARGIN)
        elif spec.layout == "image_text":
            path = self._asset_path(spec.asset_id) if spec.asset_id else None
            half = (_SLIDE_W - 3 * _MARGIN) / 2
            if path and (self.workspace / path).is_file():
                try:
                    slide.shapes.add_picture(
                        str((self.workspace / path).resolve()),
                        Inches(_MARGIN), Inches(content_top), width=Inches(half),
                    )
                except OSError as exc:
                    raise ToolError("PPT_IMAGE_EMBED_FAILED", f"图片嵌入失败: {exc}", retryable=True) from None
            _add_bullets(slide, (spec.bullets or [spec.body or "图片说明"]), _MARGIN + half + _MARGIN, content_top, half, _SLIDE_H - content_top - _MARGIN)
        elif spec.layout == "image_full":
            path = self._asset_path(spec.asset_id) if spec.asset_id else None
            if path and (self.workspace / path).is_file():
                try:
                    slide.shapes.add_picture(
                        str((self.workspace / path).resolve()),
                        Inches(_MARGIN), Inches(content_top),
                        width=Inches(_SLIDE_W - 2 * _MARGIN),
                    )
                except OSError as exc:
                    raise ToolError("PPT_IMAGE_EMBED_FAILED", f"图片嵌入失败: {exc}", retryable=True) from None
        elif spec.layout == "table":
            headers = spec.table_headers or ["列 1", "列 2"]
            rows = spec.table_rows or [["值 1", "值 2"]]
            _add_table(slide, headers, rows, _MARGIN, content_top, _SLIDE_W - 2 * _MARGIN)
        elif spec.layout == "quote":
            _add_textbox(slide, spec.body or spec.title, _MARGIN, 2.6, _SLIDE_W - 2 * _MARGIN, 2.0, 24)
        # notes
        if spec.notes:
            slide.notes_slide.notes_text_frame.text = spec.notes

    def _structural_gate(self, output: Path) -> None:
        """机械结构门：python-pptx 可打开、每页有形状、图片引用完整。"""
        try:
            prs = Presentation(str(output))
        except Exception as exc:  # noqa: BLE001 - 结构门必须拒绝坏包
            raise ToolError(
                "PPT_STRUCTURE_INVALID", f"PPTX 无法被 python-pptx 打开: {exc}"
            ) from None
        if not prs.slides:
            raise ToolError("PPT_STRUCTURE_INVALID", "PPTX 没有任何页面")
        for slide in prs.slides:
            if not list(slide.shapes):
                raise ToolError("PPT_STRUCTURE_INVALID", "存在空页面（无形状）")
            for shape in slide.shapes:
                if getattr(shape, "shape_type", None) is None:
                    continue
                if str(getattr(shape, "shape_type", "")).startswith("PICTURE"):
                    try:
                        shape.image  # noqa: B018 - 触发 image part 校验
                    except Exception as exc:  # noqa: BLE001
                        raise ToolError(
                            "PPT_STRUCTURE_INVALID", f"页面存在损坏图片引用: {exc}"
                        ) from None
        check = self.delivery_check(output)
        if check.get("ok") is not True:
            detail = str(check.get("vendor_check", ""))[:400]
            raise ToolError(
                "PPT_VENDOR_CHECK_FAILED",
                f"vendor 结构门未通过: {detail}",
                retryable=True,
                suggestion="修复生成参数后重新 ppt_generate。",
            )

    # ---------------- ppt_repair ----------------

    def repair(self, request: PptRepairRequest) -> OperationResult:
        """受限文本修改：changes 只允许 {slide_index, field, value}（不传坐标）。

        V1 无 Vision 实现不自动改写 PPTX 布局；文本修正请重新 ppt_generate。
        """
        if not request.changes:
            raise ToolError("PPT_REPAIR_EMPTY", "changes 不能为空")
        for change in request.changes:
            slide_index = int(change.get("slide_index", -1))
            if slide_index < 0:
                raise ToolError("PPT_REPAIR_INDEX", f"slide_index 非法: {slide_index}")
        raise ToolError(
            "PPT_REPAIR_NOT_AUTOMATABLE",
            "V1 无 Vision 实现不自动改写 PPTX 布局；如需文本修正请用 "
            "ppt_generate 重新生成（传入修正后的 slides）。",
        )

    # ---------------- helpers ----------------

    def _asset_path(self, asset_id: str) -> str | None:
        return self.catalog.asset_path(asset_id) if self.catalog else None

    def _write_qa(self) -> None:
        qa_dir = self.workspace / QA_DIR
        qa_dir.mkdir(parents=True, exist_ok=True)
        qa = {"mechanical": "passed", "visual": "not_run"}
        path = qa_dir / "ppt.json"
        atomic_write_json(path, qa)

    def _write_pending_visual(self, output: Path) -> None:
        qa_dir = self.workspace / QA_DIR
        qa_dir.mkdir(parents=True, exist_ok=True)
        path = self.workspace / PENDING_VISUAL
        items: list[dict[str, Any]] = []
        if path.is_file():
            try:
                items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
            except (OSError, ValueError):
                items = []
        items.append(
            {
                "step": "ppt_visual_review",
                "reason": "当前 Service 未执行真实 Vision 视觉复核，需后续检查 PPT 页面语义、审美与重叠",
                "affected_artifacts": [str(output.relative_to(self.workspace))],
                "review_required": True,
                "status": "skipped",
            }
        )
        atomic_write_json(path, {"items": items})


def _add_title(slide: Any, text: str, left: float, top: float, size: int) -> None:
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(_SLIDE_W - 2 * left), Inches(0.9))
    frame = box.text_frame
    frame.text = text or ""
    frame.paragraphs[0].font.size = Pt(size)
    frame.paragraphs[0].font.bold = True


def _add_textbox(slide: Any, text: str, left: float, top: float, width: float, height: float, size: int) -> None:
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.text_frame.text = text
    for paragraph in box.text_frame.paragraphs:
        paragraph.font.size = Pt(size)


def _add_bullets(slide: Any, bullets: list[str], left: float, top: float, width: float, height: float) -> None:
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.clear()
    for index, item in enumerate(bullets):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = f"• {item}"
        paragraph.font.size = Pt(16)


def _add_table(slide: Any, headers: list[str], rows: list[list[str]], left: float, top: float, width: float) -> None:
    shape = slide.shapes.add_table(len(rows) + 1, len(headers), Inches(left), Inches(top), Inches(width), Inches(1.6))
    table = shape.table
    for col, header in enumerate(headers):
        table.cell(0, col).text = header
    for row_index, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            if col < len(headers):
                table.cell(row_index, col).text = value


def _short_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _safe_name(value: str) -> str:
    import re

    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("._")
    return cleaned or "deck"
