"""动作直接测试，不需要版本存储或模拟渲染器。"""
import copy

import pytest
from resume_templates import TEMPLATE_IDS
from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.resume_workflow import EditRequest, Section
from skill_toolbox.resume_actions import ResumeActions
from skill_toolbox.resume_layout.profiles import get_profile


@pytest.mark.parametrize('template_id', TEMPLATE_IDS)
def test_shared_actions_preserve_input_and_apply_fields_format_and_order(template_id):
    actions = ResumeActions(get_profile(template_id))
    content = {'header': {'fields': {'name': '原姓名'}}, 'sections': [actions.from_section(Section(
        key='projects', title='项目经历', entries=[{'id': 'a', 'organization': '项目甲', 'text': ['成果甲']}]))]}
    before = copy.deepcopy(content)
    request = EditRequest(candidate_id='unused@1', changes=[
        {'op': 'update_person', 'fields': {'name': '新姓名'}},
        {'op': 'insert_entry', 'section_id': 'projects', 'entry': {'id': 'b', 'organization': '项目乙', 'text': ['成果乙']}},
        {'op': 'format', 'scope': 'entry', 'target_id': 'projects#b', 'font_size_pt': 12},
        {'op': 'move_entry', 'target_id': 'projects#b', 'before_id': 'a'},
    ])
    edited, commands, header = actions.compile(content, request.changes)
    assert content == before
    assert [e['id'] for e in edited['sections'][0]['entries']] == ['b', 'a']
    assert edited['sections'][0]['entries'][0]['font_size_pt'] == 12
    assert header['fields']['name'] == edited['header']['fields']['name'] == '新姓名'
    replayed, _ = actions.apply(content, commands)
    assert replayed['sections'] == edited['sections']
    assert actions.to_sections(edited['sections'])[0].entries[0].text == ['成果乙']


def test_failed_batch_does_not_mutate_original():
    actions = ResumeActions(get_profile('t001'))
    content = {'sections': [actions.from_section(Section(key='skills', title='技能', entries=[{'id': 's', 'text': ['Python']}]))]}
    before = copy.deepcopy(content)
    request = EditRequest(candidate_id='unused@1', changes=[
        {'op': 'rename_section', 'section_id': 'skills', 'title': '已改标题'},
        {'op': 'remove_entry', 'target_id': 'missing'},
    ])
    with pytest.raises(ToolError):
        actions.compile(content, request.changes)
    assert content == before
