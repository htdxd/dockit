"""四个真实模板的正文抽取、克隆与分列角色回归。"""
from pathlib import Path

import pytest
from skill_toolbox.resume_layout import component_body as body
from skill_toolbox.resume_layout import component_template as ct
from skill_toolbox.resume_layout import emit, layout, typography

ROOT = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates"


@pytest.mark.parametrize("template_id", ["t002", "t003", "t015", "t024"])
def test_new_project_replaces_source_and_has_independent_text_box(template_id):
    template = ROOT / template_id / "template.docx"
    spec = ct.get_spec(template_id)
    root = ct.load_normalized(template, spec)
    section = {"id": "projects", "title": "项目经历", "prototype": "entry_v1"}
    entry = {"id": "new-project", "text": "唯一项目 · owner/repo · Stars 123 · 开源\n技术栈：Python\n交付唯一成果。",
             "has_heading": True, "heading_lines": 2, "tech_stack_line": 1,
             "head": {"org": "唯一项目", "date": "2099.01", "role": "开源"}}
    parts = body._parts(root, section, entry, spec, template_id, typography.Numbering(template))
    assert len(parts) == 1
    anchor = parts[0]["anchor"]
    assert len(list(anchor.iter(emit.WPS + "wsp"))) == 1
    text = "".join(t.text or "" for t in anchor.iter(emit.W + "t"))
    assert text == entry["text"].replace("\n", "")
    assert "2099.01" not in text
    assert "中国社会大学" not in text
    assert ct.extent(anchor)[0] > 300
    for p in anchor.findall(".//" + emit.W + "txbxContent/" + emit.W + "p"):
        assert p.find(emit.W + "pPr/" + emit.W + "spacing").get(emit.W + "lineRule") == "exact"


def test_t015_work_keeps_role_date_and_organization_in_correct_parts():
    template = ROOT / "t015/template.docx"
    spec = ct.get_spec("t015")
    root = ct.load_normalized(template, spec)
    section = {"id": "work", "title": "工作经历"}
    entry = {"id": "work-one", "text": "2024.01 · 唯一公司 · 研究工程师\n完成唯一成果。",
             "has_heading": True, "heading_lines": 1,
             "head": {"org": "唯一公司", "role": "研究工程师", "date": "2024.01"}}
    parts = body._parts(root, section, entry, spec, "t015", typography.Numbering(template))
    assert {p["name"]: p["entry"]["text"] for p in parts} == {
        "side": "研究工程师\n2024.01", "main": "唯一公司\n完成唯一成果。"}
    left, right = (next(p["anchor"] for p in parts if p["name"] == name) for name in ("side", "main"))
    assert ct.position(left)[0] + ct.extent(left)[0] <= ct.position(right)[0]
    role, date = left.findall('.//' + emit.W + 'txbxContent/' + emit.W + 'p')
    assert role.find(emit.W+'pPr/'+emit.W+'numPr') is not None
    assert date.find(emit.W+'pPr/'+emit.W+'numPr') is None


@pytest.mark.parametrize("changes", [{"width_pt": 200}, {"scale": 1.25}])
def test_sidebar_width_is_bounded_by_its_own_region(changes):
    template = ROOT / "t024/template.docx"
    spec = ct.get_spec("t024")
    root = ct.load_normalized(template, spec)
    section = {"id": "summary", "title": "自我评价", "region": "sidebar"}
    entry = {"id": "wide", "text": "仅应位于左侧评价区域。", **changes}
    with pytest.raises(ValueError, match="TYPOGRAPHY_BOUNDS"):
        body._parts(root, section, entry, spec, "t024", typography.Numbering(template))


@pytest.mark.parametrize("template_id", ["t002", "t003", "t015", "t024"])
def test_emit_removes_unused_columns_and_clones_only_requested_entries(template_id, tmp_path):
    template = ROOT / template_id / "template.docx"
    scenario = {"header": {"hide_photo": True}, "sections": [{
        "id": "projects", "title": "唯一新增栏目", "entries": [
            {"id": "new-one", "text": "唯一新增条目\n唯一新增正文", "heading_lines": 1, "has_heading": True}]}]}
    ct.prepare_sections(scenario["sections"], template_id)
    entry = layout.EntryPlan("new-one", "projects", 0, 350, 65, 2, 36)
    section = layout.SectionPlan("projects", "唯一新增栏目", 0, 315, [entry])
    plan = layout.LayoutPlan(1, [section], 315, 805)
    output = tmp_path / "render.docx"
    result = body.emit_scenario(scenario, plan, output, template=template, template_id=template_id)
    root = emit.load_document_xml(output)
    text = "".join(t.text or "" for t in root.iter(emit.W + "t"))
    assert text.count("唯一新增栏目") == text.count("唯一新增条目") == 1
    assert "中国社会大学" not in text
    assert not any(not len(d) for d in root.iter(emit.W + "drawing"))
    assert len(result["parts"]) == 1
    assert result["parts"][0]["page"] == 0
