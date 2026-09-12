"""字号与比例缩放必须一致进入 XML、布局和独立 QA。"""
import copy

import pytest
from lxml import etree

from skill_toolbox.resume_layout import emit, layout, qa, t001, typography
from skill_toolbox.resume_layout.t109 import TEMPLATE as T109

T001 = T109.parent.parent / "t001/template.docx"


def body_entry(template=T001):
    root = emit.load_document_xml(template)
    body = (t001.body_for(root, {"id": "work"}) if template == T001 else
            emit.find_body_wsp(emit._anchor_by_docpr_name(root, "组合 216")))
    emit.set_box_text(body, "职位\t组织\t2025\n负责业务实现", first_is_header=True)
    return body


def test_absolute_font_size_preserves_heading_ratio_and_changes_real_line_spacing():
    body = body_entry()
    numbering = typography.Numbering(T001)
    typography.apply_body(body, {"font_size_pt": 12, "text": "职位\n正文"},
                          {"id": "work"}, "t001", numbering)
    paragraphs = body.findall(".//" + emit.W + "txbxContent/" + emit.W + "p")
    assert [p.find(emit.W + "r/" + emit.W + "rPr/" + emit.W + "sz").get(emit.W + "val")
            for p in paragraphs] == ["25", "24"]
    assert all(p.find(emit.W + "pPr/" + emit.W + "spacing").get(emit.W + "line") == "432"
               for p in paragraphs)
    assert numbering.parts()  # 独立 bullet 样式，不污染其它条目


def test_entry_scale_changes_width_and_insets_and_explicit_width_wins():
    body = body_entry()
    result = typography.apply_body(body, {"scale": .9, "text": "职位\n正文"},
                                   {"id": "work"}, "t001", typography.Numbering(T001))
    assert result["body_width_pt"] == pytest.approx(506.75 * .9)
    assert result["line_pitch_pt"] == pytest.approx(16.2)
    assert body.find(emit.WPS + "bodyPr").get("lIns") == str(round(91440 * .9))
    other = body_entry()
    result = typography.apply_body(other, {"scale": .9, "width_pt": 400, "text": "正文"},
                                   {"id": "work"}, "t001", typography.Numbering(T001))
    assert result["body_width_pt"] == 400


def test_default_typography_preserves_xml_exactly():
    body = body_entry()
    before = etree.tostring(body)
    numbering = typography.Numbering(T001)
    typography.apply_body(body, {"text": "正文"}, {"id": "work"}, "t001", numbering)
    assert etree.tostring(body) == before
    assert not numbering.parts()


def test_combined_scale_cannot_silently_create_tiny_font_or_page_overflow():
    with pytest.raises(ValueError, match="8pt"):
        typography.factors({"font_size_pt": 8, "scale": .75}, {"scale": .9}, "t001")
    with pytest.raises(ValueError, match="宽度"):
        typography.apply_body(body_entry(), {"scale": 1.2, "text": "正文"},
                              {"id": "work"}, "t001", typography.Numbering(T001))
    root = emit.load_document_xml(T109)
    anchor = emit._anchor_by_docpr_name(root, "组合 216")
    with pytest.raises(ValueError, match="右边界"):
        typography.scale_title(anchor, 1.25, body=emit.find_body_wsp(anchor), template_id="t109")


def test_section_geometry_scale_does_not_touch_photo_and_can_be_rebuilt():
    root = emit.load_document_xml(T109)
    photo = emit._anchor_by_docpr_name(root, "组合 12")
    before_photo = etree.tostring(photo)
    section = emit._anchor_by_docpr_name(root, "组合 216")
    original = copy.deepcopy(section)
    before = int(section.find(emit.WP + "extent").get("cx"))
    typography.scale_title(section, .9, body=emit.find_body_wsp(section), template_id="t109")
    assert int(section.find(emit.WP + "extent").get("cx")) == round(before * .9)
    assert etree.tostring(photo) == before_photo
    typography.scale_title(original, 1, body=emit.find_body_wsp(original), template_id="t109")
    assert int(original.find(emit.WP + "extent").get("cx")) == before


def test_qa_compares_actual_scaled_pitch_instead_of_eighteen():
    measured = [{"entry_id": "a", "wrapped_lines": 2, "text_height_pt": 32.4, "line_pitch_pt": 16.2}]
    rendered = [{"entry_id": "a", "wrapped_lines": 2, "occupied_height_pt": 32.4, "render_pitch_pt": 16.2}]
    assert not qa.check_measurement_vs_render(measured, rendered, expected_entry_ids=["a"])
    rendered[0]["render_pitch_pt"] = 18
    assert qa.check_measurement_vs_render(measured, rendered, expected_entry_ids=["a"])


def test_measured_offsets_and_width_are_carried_into_layout():
    measured = layout.MeasureResult.from_dict({
        "entry_id": "a", "wrapped_lines": 2, "text_height_pt": 32.4,
        "text_top_offset_pt": 3.5, "body_width_pt": 455,
        "body_offset_pt": 26.28, "body_pad_pt": 4.14,
    })
    plan = layout.plan_layout([{"id": "work", "scale": .9, "entries": [{"id": "a"}]}], {"a": measured})
    entry = plan.sections[0].entries[0]
    assert entry.text_top_offset_pt == 3.5 and entry.body_width_pt == 455
    assert entry.body_h_pt == pytest.approx(3.5 + 32.4 + 4.14)
    assert entry.body_offset_pt == pytest.approx(29.2 * .9)
