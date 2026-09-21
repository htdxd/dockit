from dataclasses import replace

import pymupdf as fitz

from skill_toolbox.resume_layout.component_template import plan_scenario, prepare_sections
from skill_toolbox.resume_layout.component_qa import inspect_component
from skill_toolbox.resume_layout.layout import MeasureResult
from skill_toolbox.resume_layout.profiles import get_profile


def test_sidebar_does_not_push_main_column_and_reflows_itself():
    sections = [
        {'id': 'education', 'title': 'Education', 'entries': [{'id': 'edu'}]},
        {'id': 'summary', 'title': 'Summary', 'entries': [{'id': 'summary'}]},
        {'id': 'work', 'title': 'Work', 'entries': [{'id': 'work'}]},
    ]
    prepare_sections(sections, 't024')
    measured = {key: MeasureResult(key, 3, 54) for key in ('edu', 'summary', 'work')}
    geometry = get_profile('t024').geometry
    scenario = {'sections': sections}
    before = plan_scenario(scenario, measured, template_id='t024', geometry=geometry)
    measured['summary'] = replace(measured['summary'], text_height_pt=300, wrapped_lines=20)
    after = plan_scenario(scenario, measured, template_id='t024', geometry=geometry)
    assert after.pages == 2
    assert after.sections[1].page_index == 1
    assert [(s.anchor_y_pt, s.page_index) for s in after.sections if s.region == 'main'] == [
        (s.anchor_y_pt, s.page_index) for s in before.sections if s.region == 'main']
    shifted = plan_scenario(scenario, measured, template_id='t024', geometry=geometry,
                            entry_adjust={'edu': {'height_delta_pt': 40}})
    assert shifted.sections[2].anchor_y_pt == after.sections[2].anchor_y_pt + 40


def test_pdf_measurement_is_clipped_by_column_and_counts_actual_baselines():
    doc = fitz.open()
    page = doc.new_page()
    for y, text in [(60, 'ALPHA'), (78, 'BETA'), (96, 'GAMMA')]:
        page.insert_text((50, y), text, fontsize=10)
        page.insert_text((300, y), 'OTHER COLUMN', fontsize=10)
    component = dict(x_pt=40, y_pt=40, width_pt=150, height_pt=70, measurement_id='part')
    text, row = inspect_component(page, component, {'line_pitch_pt': 18})
    assert text == 'ALPHABETAGAMMA'
    assert row['wrapped_lines'] == 3
    assert row['occupied_height_pt'] == 54
    assert row['render_pitch_pt'] == 18
    page.insert_text((50, 114), 'EXTRA', fontsize=10)
    component['height_pt'] = 90
    _, changed = inspect_component(page, component, {'line_pitch_pt': 18})
    assert changed['wrapped_lines'] == 4
