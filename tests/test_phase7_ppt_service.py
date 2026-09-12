"""阶段 7 强制测试：PPT vendor adapter + 有界生成（计划 §阶段7）。

覆盖：
- vendor_fingerprint 读上游版本/脚本存在性（契约变化可被测试发现）
- create_outline 有界大纲（固定布局池、页数限制）
- generate 固定输入生成可编辑 PPTX（python-pptx 可打开、页数、图片嵌入）
- 结构门：坏图/空页面/坏包拒绝交付
- 未知布局/未知 asset_id 拒绝
- 无 Vision：QA visual=not_run、登记 pending-visual-review、无图片 payload
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from pptx import Presentation

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.ppt import (
    PptGenerateRequest,
    PptRepairRequest,
    SlideSpec,
)
from skill_toolbox.materials import MaterialService
from skill_toolbox.tools.ppt import PptService

VENDOR = Path("backend/skill_toolbox/skill_defs/ppt-master")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def service(workspace: Path) -> PptService:
    return PptService(
        workspace,
        VENDOR,
        capabilities={"vision": False, "tool_calling": True},
    )


def _catalog_with_image(workspace: Path, tmp_path: Path) -> MaterialService:
    img = tmp_path / "photo.png"
    img.write_bytes(_png_bytes())
    material = MaterialService(workspace)
    material.prepare_material(img)
    return material


def _png_bytes(width: int = 60, height: int = 40) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([10, 80, 140]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ---------------- vendor 适配 ----------------

def test_vendor_fingerprint_reads_version_and_scripts(service: PptService) -> None:
    fp = service.vendor_fingerprint()
    assert fp["version"]  # SKILL.md / manifest 版本
    assert "4.2.0" in fp["version"]
    assert fp["scripts_present"]["project_manager.py"] is True
    assert fp["scripts_present"]["svg_to_pptx.py"] is True
    assert fp["adapter"] == "skill_toolbox/tools/ppt.py"


def test_vendor_manifest_contract(service: PptService) -> None:
    """上游 manifest 的 action 白名单与脚本存在性对齐（契约变化可被测试发现）。"""
    manifest = json.loads(
        (VENDOR / "manifest.json").read_text(encoding="utf-8")
    )
    for action, spec in manifest["scripts"].items():
        entry = (VENDOR / "scripts" / spec["entry"]).resolve()
        assert entry.is_file(), f"manifest action {action} 引用缺失脚本 {spec['entry']}"


# ---------------- create_outline ----------------

def test_create_outline_bounded_layouts(service: PptService, workspace: Path) -> None:
    result = service.create_outline("AI 产品介绍", "开发者", 7, "clean")
    assert result.ok
    assert len(result.data["slides"]) == 7
    layouts = {slide["layout"] for slide in result.data["slides"]}
    assert "title" in layouts and "end" in layouts
    assert layouts <= {
        "title", "section", "bullets", "two_column", "image_text", "image_full",
        "table", "agenda", "quote", "end",
    }
    outline_path = workspace / "work" / "outlines" / f"{result.artifact_id}.json"
    assert outline_path.is_file()


def test_create_outline_rejects_bad_page_count(service: PptService) -> None:
    with pytest.raises(ToolError) as exc:
        service.create_outline("x", page_count=31)
    assert exc.value.code == "PPT_PAGE_COUNT"


# ---------------- generate ----------------

def test_generate_builds_editable_pptx(service: PptService, workspace: Path) -> None:
    result = service.generate(
        PptGenerateRequest(
            outline_id="outline-test",
            slides=[
                SlideSpec(layout="title", title="AI 产品介绍"),
                SlideSpec(layout="bullets", title="核心能力", bullets=["能力一", "能力二"]),
                SlideSpec(
                    layout="table",
                    title="对比",
                    table_headers=["维度", "价值"],
                    table_rows=[["性能", "高"], ["成本", "低"]],
                ),
                SlideSpec(layout="end", title="谢谢"),
            ],
        )
    )
    assert result.ok
    out = workspace / result.data["path"]
    assert out.is_file()
    prs = Presentation(str(out))
    assert len(prs.slides) == 4
    # 每个页面有形状（非空页）
    for slide in prs.slides:
        assert list(slide.shapes)
    qa = json.loads(
        (workspace / "work" / "qa" / "ppt.json").read_text(encoding="utf-8")
    )
    assert qa["mechanical"] == "passed"
    assert qa["visual"] == "not_run"
    # 无 Vision：登记 pending-visual-review
    pending = json.loads(
        (workspace / "work" / "qa" / "pending-visual-review.json").read_text(
            encoding="utf-8"
        )
    )
    assert pending["items"][0]["step"] == "ppt_visual_review"
    assert pending["items"][0]["status"] == "skipped"
    assert pending["items"][0]["review_required"] is True
    # 无图片 payload
    payload = json.dumps(result.data, ensure_ascii=False)
    assert "base64" not in payload and "data:image" not in payload


def test_generate_with_image_asset(
    service: PptService, workspace: Path, tmp_path: Path
) -> None:
    material = _catalog_with_image(workspace, tmp_path)
    service.catalog = material.catalog()
    asset = next(iter(material.catalog().irs.values())).assets[0]

    result = service.generate(
        PptGenerateRequest(
            outline_id="outline-img",
            slides=[
                SlideSpec(layout="title", title="图册"),
                SlideSpec(layout="image_text", title="图片页", asset_id=asset.id, body="说明"),
            ],
        )
    )
    assert result.ok
    out = workspace / result.data["path"]
    with zipfile.ZipFile(out) as z:
        assert any(n.startswith("ppt/media/") for n in z.namelist())


def test_generate_rejects_unknown_asset(service: PptService) -> None:
    with pytest.raises(ToolError) as exc:
        service.generate(
            PptGenerateRequest(
                outline_id="x",
                slides=[SlideSpec(layout="image_text", title="t", asset_id="asset-invented")],
            )
        )
    assert exc.value.code == "PPT_ASSET_UNKNOWN"


def test_generate_rejects_unknown_layout(service: PptService) -> None:
    """非法 layout 在 typed 参数校验层直接被拒（Pydantic Literal 门）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SlideSpec(layout="freeform", title="t")  # type: ignore[arg-type]


def test_generate_empty_slides_rejected(service: PptService) -> None:
    with pytest.raises(ToolError) as exc:
        service.generate(PptGenerateRequest(outline_id="x", slides=[]))
    assert exc.value.code == "PPT_NO_SLIDES"


# ---------------- 结构门失败路径 ----------------

def test_structure_gate_rejects_empty_presentation(
    service: PptService, workspace: Path
) -> None:
    """坏包/空 PPTX → 结构门拒绝（不交付）。"""
    bad = workspace / "bad.pptx"
    bad.write_bytes(b"not a zip")
    from skill_toolbox.tools.ppt import _safe_name

    # 直接调用结构门验证坏包被拒
    with pytest.raises(ToolError) as exc:
        service._structural_gate(bad)
    assert exc.value.code == "PPT_STRUCTURE_INVALID"


# ---------------- repair ----------------

def test_repair_refuses_automatic_layout_change(service: PptService) -> None:
    with pytest.raises(ToolError) as exc:
        service.repair(
            PptRepairRequest(
                artifact_id="outline-x",
                issues=["text_overflow"],
                changes=[{"slide_index": 0, "field": "title", "value": "新标题"}],
            )
        )
    assert exc.value.code == "PPT_REPAIR_NOT_AUTOMATABLE"
