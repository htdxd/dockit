"""新模板资源完整性与语义映射的独立回归。"""
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from skill_toolbox.resume_layout import emit, t002, t003

TEMPLATES = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates"


@pytest.mark.parametrize("template_id,spec", [("t002", t002.SPEC), ("t003", t003.SPEC)])
def test_component_references_cover_real_source_shapes(template_id, spec):
    root = emit.load_document_xml(TEMPLATES / template_id / "template.docx")
    anchors = {int(a.find(emit.WP + "docPr").get("id")): a for a in root.iter(emit.WP + "anchor")}
    assert set(spec["positions"]) == set(anchors)
    sections = spec["sections"]
    for section in sections.values():
        assert section["region"] in spec["regions"]
        for ident in section["title_ids"]:
            assert ident in anchors
        body = section["body"]
        boxes = list(anchors[body["anchor"]].iter(emit.WPS + "wsp"))
        shape = boxes[body["shape"] if body["shape"] is not None else 0]
        assert shape.find(".//" + emit.W + "txbxContent") is not None
        assert "".join(shape.iter(emit.W + "t").__next__().itertext())
    for ref in spec["header_columns"]:
        boxes = list(anchors[ref["anchor"]].iter(emit.WPS + "wsp"))
        assert boxes[ref["shape"] or 0].find(".//" + emit.W + "txbxContent") is not None
    assert spec["photo"]["anchor"] in anchors
    assert set(spec["decorations"]) <= set(anchors)


@pytest.mark.parametrize("template_id,spec", [("t002", t002.SPEC), ("t003", t003.SPEC)])
def test_release_template_hash_and_safe_portrait(template_id, spec):
    template = TEMPLATES / template_id / "template.docx"
    manifest = json.loads((template.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["template_sha256"] == hashlib.sha256(template.read_bytes()).hexdigest()
    with zipfile.ZipFile(TEMPLATES / "t109/template.docx") as reference:
        expected = reference.read("word/media/image1.png")
    with zipfile.ZipFile(template) as package:
        media = [n for n in package.namelist() if n.startswith("word/media/") and not n.endswith("/")]
        assert media == [spec["photo"]["part"]]
        assert package.read(media[0]) == expected
        assert not any(n.startswith("docProps/thumbnail.") for n in package.namelist())
        types = emit.etree.fromstring(package.read("[Content_Types].xml"))
        override = next(e for e in types if e.get("PartName") == "/" + media[0])
        assert override.get("ContentType") == "image/png"


def test_independent_heading_decorations_move_with_t003_section():
    assert t003.SPEC["sections"]["education"]["title_ids"] == [11, 10, 12]
    assert set(t003.SPEC["sections"]["skills"]["title_ids"]) == {17, 18, 19, 33}
    assert not set(t003.SPEC["decorations"]) & {10, 12, 18, 19, 33}


def test_t002_body_and_header_are_distinct_group_children():
    assert all(s["body"]["shape"] == 3 for s in t002.SPEC["sections"].values())
    assert [c["shape"] for c in t002.SPEC["header_columns"]] == [0, 1]
    assert t002.SPEC["positions"][26][0] < t002.SPEC["regions"]["main"]["x_pt"]


def test_t015_head_highlights_are_projected_to_each_column():
    from skill_toolbox.resume_layout.component_body import _head_spans

    entry = {"head": {"date": "2025", "org": "机构", "role": "岗位"},
             "text": "2025\t机构\t岗位\n正文", "heading_lines": 1,
             "paragraph_styles": {0: [
                 {"field": "organization", "start": 5, "end": 7, "bold": True},
                 {"field": "role", "start": 8, "end": 10, "color": "accent"},
                 {"field": "date", "start": 0, "end": 4, "background": "light"}],
                 1: [{"field": "text", "start": 0, "end": 2, "bold": True}]}}
    main = _head_spans(entry, ("org",), "t015")
    side = _head_spans(entry, ("role", "date"), "t015")
    assert main == {0: [{"field": "organization", "start": 0, "end": 2, "bold": True}]}
    assert side == {0: [{"field": "role", "start": 0, "end": 2, "color": "accent"}],
                    1: [{"field": "date", "start": 0, "end": 4, "background": "light"}]}


def test_t015_equal_head_values_keep_distinct_highlights():
    from skill_toolbox.resume_layout.component_body import _head_spans

    entry = {"head": {"date": "同值", "org": "同值", "role": "同值"},
             "text": "同值\t同值\t同值", "heading_lines": 1,
             "paragraph_styles": {0: [{"field": "role", "start": 6, "end": 8, "bold": True}]}}
    assert _head_spans(entry, ("org",), "t015") == {}
    assert _head_spans(entry, ("role", "date"), "t015") == {
        0: [{"field": "role", "start": 0, "end": 2, "bold": True}]}


def test_t015_multiline_organization_keeps_local_spans():
    from skill_toolbox.resume_layout.component_body import _head_spans

    entry = {"head": {"date": "2025", "org": "机构\n团队", "role": "岗位"},
             "text": "2025\t机构\n团队\t岗位\n正文", "heading_lines": 2,
             "paragraph_styles": {0: [{"field": "organization", "start": 5, "end": 7}],
                                  1: [{"field": "organization", "start": 0, "end": 2},
                                      {"field": "role", "start": 3, "end": 5}]}}
    assert _head_spans(entry, ("org",), "t015") == {
        0: [{"field": "organization", "start": 0, "end": 2}],
        1: [{"field": "organization", "start": 0, "end": 2}]}
    assert _head_spans(entry, ("role", "date"), "t015") == {
        0: [{"field": "role", "start": 0, "end": 2}]}
