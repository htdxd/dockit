"""真实材料生成暴露的排版与 QA 回归；无需 Word 或模型调用。"""
import hashlib
import io

import pytest
from lxml import etree
from PIL import Image
from skill_toolbox.resume_layout import emit, layout, pipeline
from skill_toolbox.resume_layout.t109 import TEMPLATE


@pytest.mark.parametrize("size", [(203, 204), (600, 900), (900, 600)])
def test_photo_keeps_rendered_aspect_inside_existing_group(size):
    root = emit.load_document_xml(TEMPLATE)
    anchor = emit._anchor_by_docpr_name(root, "组合 12")
    pic = anchor.find(".//{http://schemas.openxmlformats.org/drawingml/2006/picture}pic")
    xfrm = pic.find(".//" + emit.A + "xfrm")
    group = anchor.find(".//" + emit.WPG + "grpSpPr/" + emit.A + "xfrm")
    original_group = etree.tostring(group)
    original_extent = etree.tostring(anchor.find(emit.WP + "extent"))
    ext, off = xfrm.find(emit.A + "ext"), xfrm.find(emit.A + "off")
    center = [int(off.get(k)) + int(ext.get(c)) / 2 for k, c in (("x", "cx"), ("y", "cy"))]
    image = io.BytesIO()
    Image.new("RGB", size).save(image, format="PNG")
    emit.replace_photo(anchor, image.getvalue())
    assert etree.tostring(group) == original_group
    assert etree.tostring(anchor.find(emit.WP + "extent")) == original_extent
    outer, inner = group.find(emit.A + "ext"), group.find(emit.A + "chExt")
    displayed = [int(ext.get(axis)) * int(outer.get(axis)) / int(inner.get(axis))
                 for axis in ("cx", "cy")]
    assert displayed[0] / displayed[1] == pytest.approx(size[0] / size[1], abs=1e-5)
    for expected, (k, c) in zip(center, (("x", "cx"), ("y", "cy"))):
        assert int(off.get(k)) + int(ext.get(c)) / 2 == pytest.approx(expected, abs=1)


def _cross_page_plan():
    sections = [
        {"id": "projects", "title": "项目经历", "entries": [{"id": "a"}, {"id": "b"}]},
        {"id": "skills", "title": "技能特长", "entries": [{"id": "c"}]},
    ]
    measured = {
        "a": layout.MeasureResult("a", 29, 522),
        "b": layout.MeasureResult("b", 5, 90),
        "c": layout.MeasureResult("c", 11, 198),
    }
    return sections, layout.plan_layout(sections, measured)


def test_next_section_uses_last_page_bottom_after_section_spans_pages():
    _, plan = _cross_page_plan()
    projects, skills = plan.sections
    assert [e.page_index for e in projects.entries] == [0, 1]
    assert plan.pages == 2
    assert skills.page_index == 1
    assert projects.gap_after_pt < 30


@pytest.mark.parametrize("parts", [
    ["学历：本科"],
    ["学", "历", "：", "本科"],
    ["学历：", "本", "科"],
])
def test_header_replacement_preserves_label_across_run_shapes(parts):
    anchor = etree.Element(emit.WPS + "wsp")
    tx = etree.SubElement(anchor, emit.W + "txbxContent")
    p = etree.SubElement(tx, emit.W + "p")
    for part in parts:
        r = etree.SubElement(p, emit.W + "r")
        etree.SubElement(r, emit.W + "t").text = part
    emit.set_info_fields(anchor, {"degree": "硕士"})
    assert "".join(p.itertext()) == "学历：硕士"
    emit.set_info_fields(anchor, {"degree": ""})
    assert "".join(p.itertext()) == "学历："


def test_each_page_has_master_decorations_and_unique_anchor_ids(tmp_path):
    before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    sections, plan = _cross_page_plan()
    for section in sections:
        for entry in section["entries"]:
            entry["text"] = "用于验证分页的正文"
    out = tmp_path / "two.docx"
    pipeline.emit_scenario({"sections": sections}, plan, out, template=TEMPLATE)
    root = emit.load_document_xml(out)
    page = 0
    decoration_counts = {}
    for paragraph in root.find(emit.W + "body"):
        page += sum(b.get(emit.W + "type") == "page" for b in paragraph.iter(emit.W + "br"))
        names = [d.get("name", "") for d in paragraph.iter(emit.WP + "docPr")]
        decoration_counts[page] = decoration_counts.get(page, 0) + sum(
            name.startswith(("矩形 213", "组合 3")) for name in names
        )
    assert decoration_counts == {0: 2, 1: 2}
    ids = [d.get("id") for d in root.iter(emit.WP + "docPr")]
    assert len(ids) == len(set(ids))
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == before
