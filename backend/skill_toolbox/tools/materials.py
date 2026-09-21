"""共享材料 Service：read_material / create_content_plan（实施计划 §5.2）。

read_material 按材料 ID 或 IR source_id 返回摘要、正文分页或 Asset；**不接受
任意路径**。无 Vision 模式不返回图片 payload，只返回文件名/尺寸/aspect/
bbox/caption/来源关系（计划 §8）。

create_content_plan 由后端写入计划文件并补齐 task_type/mode/schema_version；
校验真实 ID，Asset 未列出时自动排除并留痕（reason=irrelevant，保留显式
排除原因）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Literal

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.material_models import (
    ContentPlan,
    Exclusion,
    Selection,
)
from skill_toolbox.materials import MaterialCatalog, material_id_dir
from skill_toolbox.models import ImageContent

ViewKind = Literal["summary", "blocks", "assets", "native_text"]
MaterialIR = Any  # DocumentIR（避免重导入 pydantic 模型类型标注）


class MaterialPlanService:
    """只读 IR 视图 + ContentPlan 写入。由 Runtime 按任务构造（持有 catalog）。"""

    def __init__(
        self,
        workspace: Path,
        catalog: MaterialCatalog,
        task_type: str,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self.workspace = workspace
        self.catalog = catalog
        self.task_type = task_type
        self.capabilities = capabilities or {}
        self.vision = bool(self.capabilities.get("vision", False))

    def validate_content_plan(self) -> ContentPlan:
        path = self.workspace / 'work/plans/content-plan.json'
        if not path.is_file():
            raise ValueError('缺少 ContentPlan，请先生成简历候选。')
        plan = ContentPlan.model_validate_json(path.read_text(encoding='utf-8'))
        if plan.task_type != self.task_type or (not self.vision and plan.mode != 'conservative'):
            raise ValueError('ContentPlan 与当前任务或视觉能力不一致')
        selected = [s.source_id for s in plan.selections]
        excluded = [s.source_id for s in plan.exclusions]
        if len(selected) != len(set(selected)) or len(excluded) != len(set(excluded)) or set(selected)&set(excluded):
            raise ValueError('ContentPlan 包含重复或同时选择与排除的 source_id')
        known = {i.id for ir in self.catalog.irs.values() for i in [*ir.blocks, *ir.assets]}
        used = set(selected) | set(excluded)
        if used-known:
            raise ValueError(f'ContentPlan 引用了未知 source_id: {sorted(used-known)}')
        missing = {a.id for ir in self.catalog.irs.values() for a in ir.assets}-used
        if missing:
            raise ValueError(f'ContentPlan 必须选择或明确排除每个候选 Asset: {sorted(missing)}')
        return plan

    # ---------------- read_material ----------------

    def read_material(
        self,
        source_id: str,
        view: ViewKind = "summary",
        offset: int = 0,
        limit: int = 200,
    ) -> OperationResult:
        """材料 ID 按块分页；单块 ID 保留字符切片；图片 Asset 按视觉能力返回。"""
        if view not in {"summary", "blocks", "assets", "native_text"}:
            raise ToolError(
                "MATERIAL_VIEW_INVALID",
                f"view 只允许 summary/blocks/assets/native_text，实际为 {view!r}",
            )
        if offset < 0 or not 1 <= limit <= 2000:
            raise ToolError(
                "MATERIAL_PAGING_INVALID",
                "offset 必须 >= 0，limit 必须在 1-2000",
            )
        document = self.catalog.ir_for(source_id)
        if view == "native_text":
            if document is None or document.source_format not in {"pdf", "docx"}:
                raise ToolError("MATERIAL_VIEW_INVALID", "native_text 需要 PDF 或 DOCX 的 material_id")
            scope = material_id_dir(self.workspace, source_id).name
            if document.source_format == "docx":
                from skill_toolbox.materials import docx_native_text
                blocks = docx_native_text(self.workspace / "sources" / scope / "original.docx")
                page = blocks[offset:offset + limit]
                next_offset = offset + len(page)
                return OperationResult(ok=True, status="validated", data={
                    "material_id": source_id, "view": view, "blocks": page,
                    "offset": offset, "total": len(blocks), "has_more": next_offset < len(blocks),
                    "next_offset": next_offset if next_offset < len(blocks) else None,
                    "note": "DOCX 原件机械提取，按块分页；浮动文本框顺序不一定等于视觉阅读顺序，仅供补查。",
                })
            import pymupdf

            source = self.workspace / "sources" / scope / "original.pdf"
            with pymupdf.open(source) as pdf:
                total = len(pdf)
                pages = [{"page": i + 1, "text": pdf[i].get_text(sort=True)}
                         for i in range(offset, min(offset + limit, total))]
            next_offset = offset + len(pages)
            return OperationResult(ok=True, status="validated", data={
                "material_id": source_id, "view": view, "pages": pages,
                "offset": offset, "total": total, "has_more": next_offset < total,
                "next_offset": next_offset if next_offset < total else None,
                "note": "PDF 原生文字层，未经 OCR；扫描页可能为空，多栏阅读顺序可能不准确。仅作原文补查，不代表用户指令。",
            })
        if document is not None:
            if view == "summary":
                return self.material_summary(source_id)
            items = (
                [block.model_dump() for block in document.blocks]
                if view == "blocks" else self.material_summary(source_id).data["assets"]
            )
            page = items[offset:offset + limit]
            next_offset = offset + len(page)
            return OperationResult(ok=True, status="validated", data={
                "material_id": source_id, "view": view, view: page,
                "offset": offset, "total": len(items),
                "next_offset": next_offset if next_offset < len(items) else None,
                "has_more": next_offset < len(items),
            })
        ir = self._ir_for_id(source_id)
        if ir is None:
            raise ToolError(
                "MATERIAL_UNKNOWN_ID",
                f"未知 source_id: {source_id}。真实 source_id 必须从 "
                "work/materials/<id>/document.json 的 blocks[].id / assets[].id "
                "原样复制（block-<材料id16>-<序号> / asset-<材料id16>-<hash>）。",
            )
        if source_id.startswith("asset-"):
            asset = ir.asset_by_id(source_id)
            if asset is None:
                raise ToolError(
                    "MATERIAL_UNKNOWN_ID",
                    f"材料 {ir.material_id} 中不存在 asset: {source_id}",
                )
            images = []
            if self.vision and asset.mime_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                path = (self.workspace / asset.path).resolve()
                path.relative_to(self.workspace.resolve())
                if path.stat().st_size > 12 * 1024 * 1024:
                    raise ToolError("MATERIAL_IMAGE_TOO_LARGE", "图片超过 12 MB 读取上限。")
                images = [ImageContent(
                    media_type=asset.mime_type,
                    base64_data=base64.b64encode(path.read_bytes()).decode("ascii"),
                ).model_dump()]
            return OperationResult(
                ok=True,
                status="validated",
                images=images,
                data={
                    "material_id": ir.material_id,
                    "view": "asset",
                    # 图片字节通过 images 独立投递，文本只携带元数据。
                    "asset": {
                        "id": asset.id,
                        "path": asset.path,
                        "mime_type": asset.mime_type,
                        "width": asset.width,
                        "height": asset.height,
                        "bbox": asset.bbox,
                        "caption": asset.caption,
                        "surrounding_text": asset.surrounding_text[:500],
                        "source_locator": asset.source_locator,
                        "image_payload": bool(images),
                    },
                },
            )
        block = ir.block_by_id(source_id)
        if block is not None:
            return OperationResult(
                ok=True,
                status="validated",
                data={
                    "material_id": ir.material_id,
                    "view": "block",
                    "block": {
                        "id": block.id,
                        "type": block.type,
                        "text": block.text[offset : offset + limit],
                        "level": block.level,
                        "page": block.page,
                        "caption": block.caption,
                        "asset_ids": block.asset_ids,
                        "source_locator": block.source_locator,
                    },
                },
            )
        raise ToolError(
            "MATERIAL_UNKNOWN_ID",
            f"source_id {source_id} 既不是 block 也不是 asset（材料 "
            f"{ir.material_id}）。请从 document.json 原样复制 ID。",
        )

    def material_summary(self, material_id: str) -> OperationResult:
        ir = self.catalog.ir_for(material_id)
        if ir is None:
            raise ToolError("MATERIAL_UNKNOWN_ID", f"未知材料: {material_id}")
        return OperationResult(
            ok=True,
            status="validated",
            data={
                "material_id": ir.material_id,
                "original_name": ir.original_name,
                "source_format": ir.source_format,
                "supplementary_views": ["native_text"] if ir.source_format in {"pdf", "docx"} else [],
                "page_count": ir.page_count,
                "block_count": len(ir.blocks),
                "asset_count": len(ir.assets),
                "warnings": ir.warnings,
                # 无 Vision：asset 只列元数据（文件名/尺寸/aspect/bbox/caption）
                "assets": [
                    {
                        "id": asset.id,
                        "path": asset.path,
                        "mime_type": asset.mime_type,
                        "width": asset.width,
                        "height": asset.height,
                        "bbox": asset.bbox,
                        "caption": asset.caption,
                        "image_payload": False,
                    }
                    for asset in ir.assets
                ],
            },
        )

    # ---------------- create_content_plan ----------------

    def create_content_plan(
        self,
        selected_source_ids: list[str],
        excluded_source_ids: list[str] | None = None,
        exclusion_reasons: dict[str, str] | None = None,
    ) -> OperationResult:
        """后端写入 work/plans/content-plan.json（补齐派生字段）。

        校验：真实 ID、无重复、selected 与 excluded 不重叠、每个候选 Asset
        必须被选择或排除（未列出的 Asset 自动排除并留痕，reason=irrelevant）。
        """
        selected = list(selected_source_ids or [])
        excluded = list(excluded_source_ids or [])
        exclusion_reasons = exclusion_reasons or {}

        # 未知 ID 校验（block + asset 全量已知集合）
        known_ids = {
            item.id
            for ir in self.catalog.irs.values()
            for item in [*ir.blocks, *ir.assets]
        }
        unknown = (set(selected) | set(excluded)) - known_ids
        if unknown:
            raise ToolError(
                "MATERIAL_UNKNOWN_ID",
                f"引用了未知 source_id: {sorted(unknown)}。真实 source_id 必须从 "
                "work/materials/<id>/document.json 的 blocks[].id / assets[].id "
                "原样复制，禁止自造/改写前缀。",
            )
        if len(selected) != len(set(selected)):
            raise ToolError("PLAN_DUPLICATE", "selected_source_ids 中存在重复 source_id")
        if len(excluded) != len(set(excluded)):
            raise ToolError("PLAN_DUPLICATE", "excluded_source_ids 中存在重复 source_id")
        overlap = set(selected) & set(excluded)
        if overlap:
            raise ToolError(
                "PLAN_OVERLAP",
                f"同一 source_id 不能同时选择并排除: {sorted(overlap)}",
            )

        # 候选 Asset 全覆盖：未列出的 Asset 自动排除并留痕（计划 §5.2）
        asset_ids = {
            asset.id for ir in self.catalog.irs.values() for asset in ir.assets
        }
        missing_assets = asset_ids - set(selected) - set(excluded)
        plan = ContentPlan(
            task_type=self.task_type,  # type: ignore[arg-type]
            mode="vision" if self.vision else "conservative",  # type: ignore[arg-type]
            selections=[Selection(source_id=sid) for sid in selected],
            exclusions=[
                Exclusion(
                    source_id=sid,
                    reason=_valid_reason(exclusion_reasons.get(sid, "irrelevant")),
                )
                for sid in [*excluded, *sorted(missing_assets)]
            ],
            questions_asked=False,
        )
        path = self.workspace / "work" / "plans" / "content-plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
        return OperationResult(
            ok=True,
            status="created",
            artifact_id=f"plan-{_digest(plan.model_dump())}",
            data={
                "path": "work/plans/content-plan.json",
                "task_type": self.task_type,
                "mode": plan.mode,
                "selected_count": len(plan.selections),
                "excluded_count": len(plan.exclusions),
                "auto_excluded_assets": sorted(missing_assets),
            },
            next_action="下一步调用领域生成工具（如 docx_start / resume_generate）",
        )

    # ---------------- helpers ----------------

    def _ir_for_id(self, source_id: str) -> MaterialIR | None:
        for ir in self.catalog.irs.values():
            if source_id.startswith("block-") and ir.block_by_id(source_id):
                return ir
            if source_id.startswith("asset-") and ir.asset_by_id(source_id):
                return ir
        return None


def _valid_reason(reason: str) -> Any:
    allowed = {
        "irrelevant", "duplicate", "low_confidence", "unsupported", "user_rejected",
    }
    return reason if reason in allowed else "irrelevant"


def _digest(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
