"""续页使用自身可用区域，并保留模板装饰的显示层次。"""
from dataclasses import replace
from pathlib import Path

import pytest
from skill_toolbox.resume_layout import emit, layout, pipeline
from skill_toolbox.resume_layout.profiles import get_profile


def test_large_entry_fits_continuation_without_changing_default_capacity():
    geometry = get_profile("t001").geometry
    sections = [{"id": "work", "entries": [{"id": "large"}]}]
    measured = {"large": layout.MeasureResult("large", 34, 600)}
    with pytest.raises(layout.LayoutUnsatisfiable):
        layout.plan_layout(sections, measured, geometry=replace(geometry, continuation_page_top_pt=None))
    plan = layout.plan_layout(sections, measured, geometry=geometry)
    assert plan.pages == 2
    assert plan.sections[0].page_index == 1
    assert plan.sections[0].anchor_y_pt == 110
    assert plan.to_dict()["page_capacity_top_pt"] == 243.7
    assert plan.to_dict()["continuation_page_top_pt"] == 110


def test_continued_entry_and_next_section_use_continuation_region():
    plan = layout.plan_layout([
        {"id": "work", "entries": [{"id": "a"}, {"id": "b"}]},
        {"id": "skills", "entries": [{"id": "c"}]},
    ], {k: layout.MeasureResult(k, 1, h) for k, h in (("a", 450), ("b", 180), ("c", 36))},
        geometry=get_profile("t001").geometry)
    assert plan.sections[0].entries[1].anchor_y_pt == 110
    assert plan.sections[1].page_index == 1
    assert plan.sections[1].anchor_y_pt < 340


def test_default_t109_continuation_start_is_unchanged():
    plan = layout.plan_layout([{"id": "work", "entries": [{"id": "a"}, {"id": "b"}]}],
                              {"a": layout.MeasureResult("a", 1, 500),
                               "b": layout.MeasureResult("b", 1, 180)})
    assert plan.sections[0].entries[1].page_index == 1
    assert plan.sections[0].entries[1].anchor_y_pt == 193.35


def test_continuation_banner_text_keeps_original_z_order(tmp_path):
    template = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates/t001/template.docx"
    scenario = {"sections": [{"id": "work", "title": "工作经历",
                              "entries": [{"id": "a", "text": "第一条"}, {"id": "b", "text": "第二条"}]}]}
    plan = layout.plan_layout(scenario["sections"],
                              {"a": layout.MeasureResult("a", 1, 450),
                               "b": layout.MeasureResult("b", 1, 180)},
                              geometry=get_profile("t001").geometry)
    out = tmp_path / "two.docx"
    pipeline.emit_scenario(scenario, plan, out, template=template, template_id='t001')
    root = emit.load_document_xml(out)
    banner = next(a for a in root.iter(emit.WP + "anchor")
                  if a.find(emit.WP + "docPr").get("name", "").startswith("组合 14 Clone"))
    background = next(a for a in root.iter(emit.WP + "anchor")
                      if a.find(emit.WP + "docPr").get("name", "").startswith("圆角矩形 3 Clone"))
    assert int(banner.get("relativeHeight")) > int(background.get("relativeHeight"))
    assert "PERSONAL RESUME" in "".join(t.text or "" for t in banner.iter(emit.W + "t"))
