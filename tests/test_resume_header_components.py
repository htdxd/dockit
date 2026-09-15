"""两模板个人字段组件：标签和值一起增删改，并保留对应列。"""
from pathlib import Path

import pytest

from skill_toolbox.resume_layout import emit, t001, t109
from skill_toolbox.resume_layout.header import apply_header_components
from skill_toolbox.resume_layout.profiles import get_profile

TEMPLATES = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates"


def boxes_for(template_id):
    root = emit.load_document_xml(TEMPLATES / template_id / "template.docx")
    if template_id == "t109":
        return t109.header_boxes(root)
    return [emit.find_body_wsp(t001.anchor_by_id(root, ident)) for ident in (22, 62)]


@pytest.mark.parametrize("template_id", ["t109", "t001"])
def test_add_remove_and_rename_field_components(template_id):
    profile = get_profile(template_id)
    boxes = boxes_for(template_id)
    hidden = set(profile.header_labels.values()) - {"name", "phone", "email"}
    data = {
        "fields": {"name": "测试同学", "phone": "13800000000", "email": "test@example.com"},
        "hidden_fields": sorted(hidden),
        "custom_fields": [
            {"key": "github", "label": "GitHub", "value": "https://github.com/example"},
            {"key": "phone", "label": "联系电话", "value": "13800000000"},
        ],
    }
    records = apply_header_components(boxes, profile.header_labels, data)
    assert set(records) == {"name", "phone", "email", "github"}
    assert records["phone"]["label"] == "联系电话"
    assert records["phone"]["column"] == (0 if template_id == "t109" else 1)
    text = "".join(t.text or "" for box in boxes for t in box.iter(emit.W + "t"))
    normalized = "".join(text.split())
    assert "GitHub：https://github.com/example" in normalized
    assert "联系电话：13800000000" in normalized
    assert "学历：" not in normalized
    assert "住址：" not in normalized and "地址：" not in normalized
    # 下一版从原件构建，替换整个 custom 列表不会残留旧标签/值。
    data["custom_fields"] = []
    next_records = apply_header_components(boxes_for(template_id), profile.header_labels, data)
    assert "github" not in next_records
    assert next_records["phone"]["label"] != "联系电话"


@pytest.mark.parametrize("template_id", ["t109", "t001"])
def test_hidden_field_wins_over_custom_override_and_all_rows_can_be_removed(template_id):
    profile = get_profile(template_id)
    boxes = boxes_for(template_id)
    result = apply_header_components(boxes, profile.header_labels, {
        "hidden_fields": list(profile.header_labels.values()) + ["github"],
        "custom_fields": [{"key": "github", "label": "GitHub", "value": "example"}],
    })
    assert result == {}
    assert not "".join(t.text or "" for box in boxes for t in box.iter(emit.W + "t"))
    assert all(box.find(".//" + emit.W + "txbxContent/" + emit.W + "p") is not None for box in boxes)


@pytest.mark.parametrize("shift,wrapped", [(0, False), (3, False), (3, True)])
def test_render_alignment_rejects_shifted_values_and_continuations(tmp_path, shift, wrapped):
    import pymupdf
    from skill_toolbox.resume_layout.header import check_rendered_alignment
    path = tmp_path / "header.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 40), "Name:")
        page.insert_text((120, 40), "Alice")
        page.insert_text((40, 60), "Role:")
        page.insert_text((120 + (0 if wrapped else shift), 60), "Engineer")
        if wrapped:
            page.insert_text((120 + shift, 80), "Developer")
        pdf.save(path)
    records = {"name": {"label": "Name", "after": "Alice", "column": 0},
               "role": {"label": "Role", "after": "Engineer" + ("Developer" if wrapped else ""), "column": 0}}
    issues = check_rendered_alignment(path, records)
    assert bool(issues) == bool(shift)
    assert all(issue.severity == "error" for issue in issues)


def test_body_start_follows_text_and_respects_photo_decoration(tmp_path):
    import pymupdf
    from skill_toolbox.resume_layout.header import body_start_from_render
    starts = []
    for y, photo in [(60, False), (100, False), (60, True)]:
        path = tmp_path / f"{y}-{photo}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((40, y), "Name: Alice")
            if photo:
                page.draw_rect((300, 40, 370, 150), fill=(1, 1, 1))
            pdf.save(path)
        starts.append(body_start_from_render(path, {"name": {"label": "Name", "after": "Alice"}}, 200))
    assert starts[1] - starts[0] == pytest.approx(40)
    assert starts[2] == pytest.approx(158.5)
    assert max(starts) < 200


def test_t109_photo_tracks_header_without_crossing_top_band():
    from skill_toolbox.resume_layout.t109 import photo_top_for_header
    short = photo_top_for_header(140, 110, 36, 9, 9)
    tall = photo_top_for_header(185, 110, 36, 9, 9)
    assert short - 9 == 44  # 色带以下 8pt，包括装饰外沿。
    assert tall + 110 + 9 == 185
    assert tall > short


@pytest.mark.parametrize("size", [(206, 210), (200, 300), (300, 200)])
def test_t001_compact_photo_keeps_aspect_and_updates_outer_bounds(tmp_path, size):
    import io
    from PIL import Image
    from skill_toolbox.resume_layout.layout import LayoutPlan

    image = io.BytesIO()
    Image.new("RGB", size).save(image, format="PNG")
    output = tmp_path / "photo.docx"
    result = t001.emit_scenario({"sections": [], "header": {
        "photo_bytes": image.getvalue(), "fit": {"photo_height_pt": 72.0}}},
        LayoutPlan(1, [], 243.7, 815.0), output,
        template=TEMPLATES / "t001" / "template.docx")
    photo = t001.anchor_by_id(emit.load_document_xml(output), 5)
    extent = photo.find(emit.WP + "extent")
    width, height = [emit.emu2pt(extent.get(key)) for key in ("cx", "cy")]
    assert height <= 72.01
    assert width / height == pytest.approx(size[0] / size[1], abs=0.001)
    assert result["header"]["photo"]["height_pt"] == pytest.approx(height, abs=0.01)
    assert photo.find(".//" + emit.A + "srcRect") is None
