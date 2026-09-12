"""材料整篇读取和实际图片必须可经领域工具发现并使用。"""
import base64
import json
from unittest.mock import Mock

import pytest
from PIL import Image

from skill_toolbox.materials import MaterialService
from skill_toolbox.llm_tools.common import shared_tools
from skill_toolbox.llm_tools.dispatcher import DomainServices, dispatch_with_media
from skill_toolbox.runtime import AgentRuntime
from skill_toolbox.tools.materials import MaterialPlanService


@pytest.fixture
def material(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (8, 10), "blue").save(source / "photo.png")
    text = "完整正文" * 100
    (source / "resume.md").write_text(f"# 简历\n\n{text}\n\n![照片](photo.png)\n\n最后一段。", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = MaterialService(workspace)
    service.prepare_material(source / "resume.md")
    catalog = service.catalog()
    ir = next(iter(catalog.irs.values()))
    return workspace, catalog, ir


def test_material_blocks_are_paginated_without_truncating_text(material):
    workspace, catalog, ir = material
    svc = MaterialPlanService(workspace, catalog, "resume")
    first = svc.read_material(ir.material_id, view="blocks", limit=2).data
    rest = svc.read_material(ir.material_id, view="blocks", offset=first["next_offset"]).data
    blocks = first["blocks"] + rest["blocks"]
    assert blocks == [block.model_dump() for block in ir.blocks]
    assert first["has_more"] and not rest["has_more"]
    assert rest["next_offset"] is None
    assert max(len(block["text"]) for block in blocks) > 200
    assert svc.read_material(ir.material_id).data["block_count"] == len(ir.blocks)
    assert svc.read_material(ir.material_id, view="assets").data["assets"][0]["id"] == ir.assets[0].id


def test_individual_block_keeps_character_slice_semantics(material):
    workspace, catalog, ir = material
    svc = MaterialPlanService(workspace, catalog, "resume")
    block = next(block for block in ir.blocks if len(block.text) > 200)
    assert svc.read_material(block.id, offset=2, limit=7).data["block"]["text"] == block.text[2:9]


@pytest.mark.parametrize("vision", [False, True])
def test_asset_visual_payload_reaches_dispatcher_only_with_vision(material, vision):
    workspace, catalog, ir = material
    svc = MaterialPlanService(workspace, catalog, "resume", {"vision": vision})
    text, images, meta = dispatch_with_media("read_material", {
        "source_id": ir.assets[0].id, "view": "assets",
    }, DomainServices(materials=svc))
    assert meta["ok"]
    assert json.loads(text)["data"]["asset"]["image_payload"] is vision
    assert len(images) == int(vision)
    if vision:
        assert images[0]["media_type"] == "image/png"
        assert base64.b64decode(images[0]["base64_data"]) == (workspace / ir.assets[0].path).read_bytes()
        assert images[0]["base64_data"] not in text


def test_banner_and_tool_schema_explain_whole_material_access(material):
    _, catalog, ir = material
    runtime = AgentRuntime(provider=Mock(), emit=Mock())
    banner = runtime._materials_banner(catalog, domain_mode=True)
    assert f'read_material(source_id="{ir.material_id}", view="blocks")' in banner
    assert 'read_material(' not in runtime._materials_banner(catalog)
    spec = next(tool for tool in shared_tools() if tool["name"] == "read_material")
    assert "material_id" in spec["description"]
    assert "字符数" in spec["input_schema"]["properties"]["limit"]["description"]
