"""阶段 3 强制测试：read_material / create_content_plan 工具化（计划 §5.2）。

覆盖：
- 真实 ID 读取 block/asset；未知 ID 明确报错（不接受任意路径）
- 无 Vision 不返回图片 payload（只返回元数据）
- create_content_plan 由后端写入并补齐 task_type/mode/schema_version
- 重复 / 未知 ID / selected 与 excluded 重叠被拒绝
- Asset 未列出时自动排除并留痕（reason=irrelevant）
- vision=false 时 mode=conservative
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.materials import MaterialService
from skill_toolbox.tools.materials import MaterialPlanService


@pytest.fixture
def catalog(tmp_path: Path) -> MaterialService:
    """构造含 md 材料 + 一张图的 catalog。"""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    source = source_dir / "notes.md"
    source.write_text(
        "# 笔记\n\n![图](fig.png)\n\n正文段落。\n\n- 条目一\n- 条目二",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = MaterialService(workspace)
    service.prepare_material(source)
    return service


def _plan_service(service: MaterialService, vision: bool = False) -> MaterialPlanService:
    return MaterialPlanService(
        service.workspace,
        service.catalog(),
        "docx",
        {"vision": vision, "tool_calling": True},
    )


# ---------------- read_material ----------------

def test_read_block_by_real_id(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    ir = next(iter(catalog.catalog().irs.values()))
    heading = next(b for b in ir.blocks if b.type == "heading")

    result = svc.read_material(heading.id, view="blocks")

    assert result.ok
    assert result.data["block"]["id"] == heading.id
    assert result.data["block"]["text"] == "笔记"


def test_read_asset_metadata_without_payload(catalog: MaterialService) -> None:
    svc = _plan_service(catalog, vision=False)
    ir = next(iter(catalog.catalog().irs.values()))
    asset = ir.assets[0]

    result = svc.read_material(asset.id, view="assets")

    assert result.ok
    data = result.data["asset"]
    assert data["id"] == asset.id
    assert data["image_payload"] is False, "无 Vision 不得返回图片 payload"
    assert "path" in data and "width" in data and "height" in data


def test_read_material_rejects_unknown_id(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    with pytest.raises(ToolError) as exc:
        svc.read_material("invented-block-id")
    assert exc.value.code == "MATERIAL_UNKNOWN_ID"
    assert "原样复制" in str(exc.value)


def test_read_material_never_accepts_paths(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    with pytest.raises(ToolError) as exc:
        svc.read_material("work/materials/foo/document.json", view="blocks")
    assert exc.value.code == "MATERIAL_UNKNOWN_ID"


def test_material_summary_lists_asset_metadata(catalog: MaterialService) -> None:
    svc = _plan_service(catalog, vision=False)
    ir = next(iter(catalog.catalog().irs.values()))

    result = svc.material_summary(ir.material_id)

    assert result.ok
    assert result.data["asset_count"] == 1
    assert result.data["assets"][0]["image_payload"] is False


# ---------------- create_content_plan ----------------

def test_create_plan_writes_file_with_derived_fields(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    ir = next(iter(catalog.catalog().irs.values()))
    heading = next(b for b in ir.blocks if b.type == "heading")
    asset = ir.assets[0]

    result = svc.create_content_plan(
        [heading.id], [asset.id], {"asset-ignored": "irrelevant"}
    )

    assert result.ok
    assert result.data["mode"] == "conservative"
    plan_path = catalog.workspace / "work" / "plans" / "content-plan.json"
    assert plan_path.is_file()
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    assert raw["task_type"] == "docx"
    assert raw["schema_version"] == "1"
    assert raw["mode"] == "conservative"
    assert [s["source_id"] for s in raw["selections"]] == [heading.id]
    assert {e["source_id"] for e in raw["exclusions"]} == {asset.id}


def test_create_plan_auto_excludes_unlisted_assets(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    ir = next(iter(catalog.catalog().irs.values()))
    heading = next(b for b in ir.blocks if b.type == "heading")
    asset = ir.assets[0]

    # 只选 block，不列 asset → asset 自动进入 exclusions 并留痕
    result = svc.create_content_plan([heading.id], [])

    assert result.ok
    assert asset.id in result.data["auto_excluded_assets"]
    raw = json.loads(
        (catalog.workspace / "work" / "plans" / "content-plan.json").read_text(
            encoding="utf-8"
        )
    )
    auto = next(e for e in raw["exclusions"] if e["source_id"] == asset.id)
    assert auto["reason"] == "irrelevant"


def test_create_plan_rejects_unknown_and_duplicate(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    ir = next(iter(catalog.catalog().irs.values()))
    heading = next(b for b in ir.blocks if b.type == "heading")

    with pytest.raises(ToolError) as exc:
        svc.create_content_plan(["bogus-id"], [])
    assert exc.value.code == "MATERIAL_UNKNOWN_ID"

    with pytest.raises(ToolError) as exc:
        svc.create_content_plan([heading.id, heading.id], [])
    assert exc.value.code == "PLAN_DUPLICATE"

    with pytest.raises(ToolError) as exc:
        svc.create_content_plan([heading.id], [heading.id])
    assert exc.value.code == "PLAN_OVERLAP"


def test_create_plan_vision_false_uses_conservative(catalog: MaterialService) -> None:
    svc = _plan_service(catalog, vision=False)
    ir = next(iter(catalog.catalog().irs.values()))
    block = ir.blocks[0]

    svc.create_content_plan([block.id], [])

    raw = json.loads(
        (catalog.workspace / "work" / "plans" / "content-plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["mode"] == "conservative"


def test_create_plan_explicit_exclusion_reason_kept(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    ir = next(iter(catalog.catalog().irs.values()))
    block = ir.blocks[0]
    asset = ir.assets[0]

    svc.create_content_plan(
        [block.id], [asset.id], {asset.id: "low_confidence"}
    )

    raw = json.loads(
        (catalog.workspace / "work" / "plans" / "content-plan.json").read_text(
            encoding="utf-8"
        )
    )
    exclusion = next(e for e in raw["exclusions"] if e["source_id"] == asset.id)
    assert exclusion["reason"] == "low_confidence"


def test_create_plan_invalid_reason_falls_back(catalog: MaterialService) -> None:
    svc = _plan_service(catalog)
    ir = next(iter(catalog.catalog().irs.values()))
    block = ir.blocks[0]
    asset = ir.assets[0]

    svc.create_content_plan([block.id], [asset.id], {asset.id: "not-a-reason"})

    raw = json.loads(
        (catalog.workspace / "work" / "plans" / "content-plan.json").read_text(
            encoding="utf-8"
        )
    )
    exclusion = next(e for e in raw["exclusions"] if e["source_id"] == asset.id)
    assert exclusion["reason"] == "irrelevant"
