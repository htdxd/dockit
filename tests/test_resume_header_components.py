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
