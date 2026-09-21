"""真实模板的头部抽取、隐藏、追加及照片几何回归。"""
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from resume_templates import TEMPLATE_IDS
from skill_toolbox.resume_layout import component_header as header
from skill_toolbox.resume_layout import component_template as template
from skill_toolbox.resume_layout import emit

ROOT = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_header_replace_hide_and_add_without_source_fields(template_id):
    spec = template.get_spec(template_id)
    source = ROOT / template_id / "template.docx"
    root = template.load_normalized(source, spec)
    payload = {
        "fields": {"name": "头部唯一姓名", "phone": "13912345678", "target_role": "目标职位唯一文本"},
        "hidden_fields": ["email"],
        "custom_fields": [{"key": "portfolio", "label": "作品", "value": "portfolio.example/test"}],
        "hide_photo": True,
    }
    result = header.apply_header(root, source, spec, payload, {})
    assert result["fields"]["name"]["after"] == "头部唯一姓名"
    assert result["fields"]["target_role"]["after"] == "目标职位唯一文本"
    assert result["fields"]["phone"]["after"] == "13912345678"
    assert result["fields"]["portfolio"]["after"] == "portfolio.example/test"
    assert "email" not in result["fields"]
    assert all("column" in field for field in result["fields"].values())
    assert not any(not len(node) for node in root.iter(emit.W + "drawing"))
    anchors = [a for a in root.iter(emit.WP + "anchor")
               if a.find(emit.WP + "docPr").get("name", "").startswith("ResumeHeader_")]
    text = "".join(t.text or "" for a in anchors for t in a.iter(emit.W + "t"))
    assert text.count("头部唯一姓名") == 1
    assert text.count("目标职位唯一文本") == 1
    assert "邮箱" not in text
    assert all(len(list(a.iter(emit.WPS + "wsp"))) == 1 for a in anchors)


def test_sidebar_plain_name_hidden_does_not_shift_target_paragraph():
    spec = template.get_spec("t024")
    source = ROOT / "t024/template.docx"
    root = template.load_normalized(source, spec)
    result = header.apply_header(root, source, spec, {
        "fields": {"target_role": "保留岗位"}, "hidden_fields": ["name"]}, {})
    assert "name" not in result["fields"]
    assert result["fields"]["target_role"]["standalone"] is True
    assert result["fields"]["target_role"]["after"] == "保留岗位"


@pytest.mark.parametrize("dimensions", [(600, 200), (200, 600)])
def test_replacement_photo_keeps_ratio_and_records_real_media_type(dimensions):
    spec = template.get_spec("t024")
    source = ROOT / "t024/template.docx"
    root = template.load_normalized(source, spec)
    buffer = BytesIO()
    Image.new("RGB", dimensions, "#99aabb").save(buffer, "PNG")
    overrides = {}
    result = header.apply_header(root, source, spec, {"photo_bytes": buffer.getvalue()}, overrides)
    photo = result["photo"]
    assert photo["width_pt"] / photo["height_pt"] == pytest.approx(dimensions[0] / dimensions[1], abs=0.001)
    assert overrides[spec["photo"]["part"]] == buffer.getvalue()
    types = emit.etree.fromstring(overrides["[Content_Types].xml"])
    assert next(n for n in types if n.get("PartName") == "/" + spec["photo"]["part"]).get("ContentType") == "image/png"


def test_sidebar_fit_does_not_move_main_region_or_plain_styles():
    spec = template.get_spec("t024")
    source = ROOT / "t024/template.docx"
    root = template.load_normalized(source, spec)
    originals = {ref["title_ids"][0]: template.position(template.anchor(root, ref["title_ids"][0]))
                 for ref in spec["sections"].values() if ref["region"] == "main"}
    fit = {"columns": {"column_0": {"x_pt": 16.45, "y_pt": 301.2, "width_pt": 147.8, "height_pt": 100}},
           "titles": {"44": 417.2}, "body_top_by_region": {"main": 37.8, "sidebar": 550}}
    header.apply_header(root, source, spec, {"fit": fit, "fields": {"name": "样式保留"}}, {})
    for ident, position in originals.items():
        assert template.position(template.anchor(root, ident)) == position
    name = next(a for a in root.iter(emit.WP + "anchor")
                if a.find(emit.WP + "docPr").get("name") == "ResumeHeader_plain_3_None")
    first = name.find(".//" + emit.W + "txbxContent/" + emit.W + "p")
    assert first.find(".//" + emit.W + "sz").get(emit.W + "val") == "40"


def test_standalone_header_qa_matches_value_without_synthetic_colon(tmp_path):
    import pymupdf
    from skill_toolbox.resume_layout.header import (
        check_rendered_alignment,
        rendered_field_bounds,
    )

    path = tmp_path / "plain.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), "Known Name")
        page.insert_text((70, 80), "Target Role")
        doc.save(path)
    records = {
        "name": {"label": "", "after": "Known Name", "column": "plain", "standalone": True},
        "target_role": {"label": "", "after": "Target Role", "column": "plain", "standalone": True},
    }
    assert check_rendered_alignment(path, records) == []
    with pymupdf.open(path) as doc:
        assert len(rendered_field_bounds(doc[0], records)) == len("KnownNameTargetRole")
    records["name"]["after"] = "Missing Name"
    assert check_rendered_alignment(path, records)
    with pymupdf.open(path) as doc, pytest.raises(ValueError, match="HEADER_CONTENT_MISSING"):
        rendered_field_bounds(doc[0], records)
