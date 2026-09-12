"""阶段 5 强制测试：DOCX 领域 Service（草稿 + 顺序 blocks + 质量门）。

覆盖计划 §阶段5 验收：
- docx_start 创建草稿；add_blocks 顺序追加并校验（图片只传 asset_id）
- 未知 asset_id / 空 blocks 明确拒绝
- finalize：build → (TOC) → postcheck 机械门；产物存在
- 机械门失败（占位符残留）→ DOCX_CHECK_FAILED，不交付
- docx_repair 修复被点名 block 后重新 finalize 通过
- 无 Vision：QA visual=not_run，结果无图片 payload
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.docx import (
    DocxAddBlocksRequest,
    DocxBlock,
    DocxFinalizeRequest,
    DocxRepairRequest,
    DocxStartRequest,
)
from skill_toolbox.materials import MaterialService
from skill_toolbox.tools.docx import DocxService

SCRIPTS = Path("backend/skill_toolbox/skill_defs/docx_pro/scripts")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def service(workspace: Path) -> DocxService:
    return DocxService(
        workspace,
        SCRIPTS,
        capabilities={"vision": False, "tool_calling": True},
    )


def _catalog_with_image(workspace: Path, tmp_path: Path) -> MaterialService:
    img = tmp_path / "fig.png"
    img.write_bytes(_png_bytes())
    material_service = MaterialService(workspace)
    material_service.prepare_material(img)
    return material_service


def _png_bytes(width: int = 64, height: int = 48) -> bytes:
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
    raw = b"".join(b"\x00" + bytes([200, 100, 50]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _blocks_with_image(catalog: MaterialService) -> list[DocxBlock]:
    asset = next(iter(catalog.catalog().irs.values())).assets[0]
    return [
        DocxBlock(type="heading", text="第一章 引言", level=1),
        DocxBlock(type="paragraph", text="第一段正文"),
        DocxBlock(type="image", asset_id=asset.id, caption="图 1 流程"),
        DocxBlock(
            type="table",
            headers=["能力", "状态"],
            rows=[["表格", "通过"]],
            caption="表 1",
        ),
        DocxBlock(type="formula", latex="E = mc^2", caption="质能方程"),
    ]


# ---------------- start / add_blocks ----------------

def test_start_creates_draft(service: DocxService, workspace: Path) -> None:
    result = service.start(
        DocxStartRequest(title="测试文档", complexity="simple")
    )
    assert result.ok
    assert result.data["document_id"].startswith("docx-")
    draft_path = workspace / "work" / "drafts" / f"{result.artifact_id}.json"
    assert draft_path.is_file()


def test_add_blocks_appends_in_order(
    service: DocxService, workspace: Path, tmp_path: Path
) -> None:
    catalog = _catalog_with_image(workspace, tmp_path)
    service.catalog = catalog.catalog()
    started = service.start(DocxStartRequest(title="顺序 block", complexity="simple"))

    result = service.add_blocks(
        DocxAddBlocksRequest(
            document_id=started.artifact_id,
            blocks=_blocks_with_image(catalog),
        )
    )
    assert result.ok
    assert result.data["total_blocks"] == 5
    draft = json.loads(
        (workspace / "work" / "drafts" / f"{started.artifact_id}.json").read_text(
            encoding="utf-8"
        )
    )
    types = [block["type"] for block in draft["blocks"]]
    assert types == ["heading", "paragraph", "image", "table", "formula"]
    # 图片 block 的 source 解析为真实 asset 路径
    image_block = next(b for b in draft["blocks"] if b["type"] == "image")
    assert (workspace / image_block["source"]).is_file()


def test_add_blocks_rejects_unknown_asset(
    service: DocxService, workspace: Path
) -> None:
    started = service.start(DocxStartRequest(title="x", complexity="simple"))
    with pytest.raises(ToolError) as exc:
        service.add_blocks(
            DocxAddBlocksRequest(
                document_id=started.artifact_id,
                blocks=[DocxBlock(type="image", asset_id="asset-invented-000000")],
            )
        )
    assert exc.value.code == "DOCX_ASSET_UNKNOWN"


def test_add_blocks_rejects_empty_and_invalid(
    service: DocxService, workspace: Path
) -> None:
    started = service.start(DocxStartRequest(title="x", complexity="simple"))
    with pytest.raises(ToolError) as exc:
        service.add_blocks(
            DocxAddBlocksRequest(document_id=started.artifact_id, blocks=[])
        )
    assert exc.value.code == "DOCX_EMPTY_BLOCKS"
    with pytest.raises(ToolError) as exc:
        service.add_blocks(
            DocxAddBlocksRequest(
                document_id=started.artifact_id,
                blocks=[DocxBlock(type="table", headers=[], rows=[])],
            )
        )
    assert exc.value.code == "DOCX_BLOCK_INVALID"


# ---------------- finalize（成功路径） ----------------

def test_finalize_success_with_image_table_formula(
    service: DocxService, workspace: Path, tmp_path: Path
) -> None:
    catalog = _catalog_with_image(workspace, tmp_path)
    service.catalog = catalog.catalog()
    started = service.start(DocxStartRequest(title="报告", complexity="simple"))
    service.add_blocks(
        DocxAddBlocksRequest(
            document_id=started.artifact_id, blocks=_blocks_with_image(catalog)
        )
    )

    result = service.finalize(
        DocxFinalizeRequest(document_id=started.artifact_id, output_name="报告.docx")
    )

    assert result.ok
    out = workspace / "artifacts" / "报告.docx"
    assert out.is_file()
    # 图片真实嵌入 + OMML 公式
    with zipfile.ZipFile(out) as z:
        assert any(n.startswith("word/media/") for n in z.namelist())
        assert "oMath" in z.read("word/document.xml").decode("utf-8")
    qa = json.loads(
        (workspace / "work" / "qa" / "docx.json").read_text(encoding="utf-8")
    )
    assert qa["mechanical"] == "passed"
    assert qa["visual"] == "not_run"  # 无 Vision


def test_finalize_standard_injects_toc(
    service: DocxService, workspace: Path
) -> None:
    started = service.start(DocxStartRequest(title="长文档", complexity="standard"))
    blocks = [
        DocxBlock(type="heading", text=f"第 {i} 章", level=1)
        for i in range(1, 5)
    ]
    service.add_blocks(
        DocxAddBlocksRequest(document_id=started.artifact_id, blocks=blocks)
    )

    result = service.finalize(
        DocxFinalizeRequest(document_id=started.artifact_id, output_name="toc.docx")
    )

    assert result.ok
    out = workspace / "artifacts" / "toc.docx"
    assert out.is_file()
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "TOC" in xml or "目录" in xml


# ---------------- 机械门失败路径 ----------------

def test_finalize_rejects_placeholder_text(service: DocxService, workspace: Path) -> None:
    """正文残留占位符 → postcheck 机械门拒绝 finalize（QA 不写 passed）。"""
    started = service.start(DocxStartRequest(title="坏文档", complexity="simple"))
    service.add_blocks(
        DocxAddBlocksRequest(
            document_id=started.artifact_id,
            blocks=[DocxBlock(type="paragraph", text="正文包含 {{name}} 占位符")],
        )
    )
    with pytest.raises(ToolError) as exc:
        service.finalize(
            DocxFinalizeRequest(document_id=started.artifact_id, output_name="bad.docx")
        )
    assert exc.value.code == "DOCX_CHECK_FAILED"
    # build 中间产物存在是合法的；机械门拒绝的是"注册为已通过"（QA 不写 passed）
    assert not (workspace / "work" / "qa" / "docx.json").exists()


def test_finalize_empty_blocks_produces_valid_docx(
    service: DocxService, workspace: Path
) -> None:
    """空 blocks 也生成合法文档（postcheck 0 error，简单场景可交付）。"""
    started = service.start(DocxStartRequest(title="空文档", complexity="simple"))
    result = service.finalize(
        DocxFinalizeRequest(document_id=started.artifact_id, output_name="empty.docx")
    )
    assert result.ok
    assert (workspace / "artifacts" / "empty.docx").is_file()


def test_finalize_unknown_draft(service: DocxService) -> None:
    with pytest.raises(ToolError) as exc:
        service.finalize(
            DocxFinalizeRequest(document_id="docx-nope", output_name="x.docx")
        )
    assert exc.value.code == "DOCX_NO_DRAFT"


# ---------------- repair ----------------

def test_repair_replaces_placeholder_then_finalizes(
    service: DocxService, workspace: Path
) -> None:
    started = service.start(DocxStartRequest(title="修复文档", complexity="simple"))
    service.add_blocks(
        DocxAddBlocksRequest(
            document_id=started.artifact_id,
            blocks=[DocxBlock(type="paragraph", text="正文包含 {{name}} 占位符")],
        )
    )
    with pytest.raises(ToolError):
        service.finalize(
            DocxFinalizeRequest(document_id=started.artifact_id, output_name="repair.docx")
        )

    result = service.repair(
        DocxRepairRequest(
            artifact_id=started.artifact_id,
            issue_id="placeholder",
            fix={
                "type": "replace_block_text",
                "block_index": 0,
                "text": "正文内容已修正",
                "output_name": "repair.docx",
            },
        )
    )
    assert result.ok
    out = workspace / "artifacts" / "repair.docx"
    assert out.is_file()
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "占位符" not in xml
    assert "已修正" in xml


def test_repair_unsupported_fix(service: DocxService, workspace: Path) -> None:
    started = service.start(DocxStartRequest(title="x", complexity="simple"))
    with pytest.raises(ToolError) as exc:
        service.repair(
            DocxRepairRequest(
                artifact_id=started.artifact_id,
                issue_id="x",
                fix={"type": "arbitrary_edit", "path": "work/spec.json"},
            )
        )
    assert exc.value.code == "DOCX_REPAIR_UNSUPPORTED"


# ---------------- 无 Vision payload ----------------

def test_no_vision_no_image_payload(service: DocxService, workspace: Path, tmp_path: Path) -> None:
    catalog = _catalog_with_image(workspace, tmp_path)
    service.catalog = catalog.catalog()
    started = service.start(DocxStartRequest(title="payload", complexity="simple"))
    service.add_blocks(
        DocxAddBlocksRequest(
            document_id=started.artifact_id, blocks=_blocks_with_image(catalog)
        )
    )
    result = service.finalize(
        DocxFinalizeRequest(document_id=started.artifact_id, output_name="p.docx")
    )
    payload = json.dumps(result.data, ensure_ascii=False)
    assert "base64" not in payload
    assert "data:image" not in payload
    assert result.data["visual"] == "not_run"
