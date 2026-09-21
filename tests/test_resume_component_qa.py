"""共享渲染验收的失败样例；实际 PDF，不模拟 PDF 提取器。"""
import copy

import pymupdf as fitz
import pytest
from skill_toolbox.resume_layout.component_qa import check_components


def sample(tmp_path, *, text=('Alpha', 'Beta'), pitch=18, title='Experience'):
    path = tmp_path / 'sample.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 30), title, fontsize=10)
        for index, line in enumerate(text):
            page.insert_text((50, 60 + index * pitch), line, fontsize=10)
        doc.save(path)
    scenario = {'sections': [{'id': 'work', 'title': 'Experience', 'entries': [
        {'id': 'job', 'text': 'Alpha\nBeta'}]}]}
    plan = {'pages': 1, 'page_capacity_bottom_pt': 800}
    emitted = {'components': [{'id': 'job::main', 'entry_id': 'job', 'measurement_id': 'job::main',
        'page_index': 0, 'text': 'Alpha\nBeta', 'x_pt': 40, 'y_pt': 40,
        'width_pt': 200, 'height_pt': 90}], 'fixed_texts': ['Experience']}
    measured = [{'entry_id': 'job::main', 'wrapped_lines': 2, 'text_height_pt': 36, 'line_pitch_pt': 18}]
    return path, scenario, plan, emitted, measured, {}


def test_valid_component_and_full_document_agree(tmp_path):
    assert check_components(*sample(tmp_path))['passed']


@pytest.mark.parametrize('text', [('Alpha',), ('Beta', 'Alpha'), ('Alpha', 'Alpha', 'Beta')])
def test_missing_reordered_or_repeated_body_is_rejected(tmp_path, text):
    report = check_components(*sample(tmp_path, text=text))
    assert not report['passed']
    assert any(i['check'] == 'content_completeness' for i in report['issues'])


def test_line_pitch_is_measured_from_pdf(tmp_path):
    report = check_components(*sample(tmp_path, pitch=21))
    assert not report['passed']
    assert any(i['check'] == 'measurement_vs_render' for i in report['issues'])


def test_missing_title_cannot_hide_in_emitted_whitelist(tmp_path):
    args = list(sample(tmp_path, title='Wrong'))
    args[3]['fixed_texts'] = ['Wrong']
    report = check_components(*args)
    assert any(i['check'] == 'title_uniqueness' for i in report['issues'])


def test_component_split_cannot_drop_input_before_render(tmp_path):
    args = list(sample(tmp_path))
    args[1]['sections'][0]['entries'][0]['text'] += '\nMissing original fact'
    report = check_components(*args)
    assert any(i['check'] == 'component_content' for i in report['issues'])


def test_empty_or_duplicate_measurements_do_not_pass(tmp_path):
    args = list(sample(tmp_path))
    args[4].append(copy.deepcopy(args[4][0]))
    assert not check_components(*args)['passed']
    args[4] = []
    assert not check_components(*args)['passed']


def test_real_pdf_overlap_is_rejected(tmp_path):
    args = sample(tmp_path)
    with fitz.open(args[0]) as doc:
        doc[0].insert_text((55, 60), 'COLLISION', fontsize=10)
        doc.saveIncr()
    report = check_components(*args)
    assert any(i['check'] == 'no_overlap' for i in report['issues'])


def test_component_outside_page_is_rejected(tmp_path):
    args = sample(tmp_path)
    args[3]['components'][0]['x_pt'] = -1
    report = check_components(*args)
    assert any(i['check'] == 'within_page' for i in report['issues'])
