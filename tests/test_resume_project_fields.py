import copy
from pathlib import Path
import pytest

from skill_toolbox.contracts.resume_workflow import Entry
from skill_toolbox.resume_content import canonical_entry


def test_explicit_stack_and_legacy_stack_have_same_canonical_shape():
    section = {"key": "projects", "title": "项目经历"}
    old = {"head": {"org": "个人项目 | 独立开发", "role": "学习平台"},
           "bullets": ["技术栈：Python、FastAPI", "独立实现资料解析与检索。"]}
    original = copy.deepcopy(old)
    result = canonical_entry(section, old)
    assert result["head"] == {"org": "学习平台", "role": "个人项目 | 独立开发"}
    assert result["tech_stack"] == "Python、FastAPI"
    assert result["bullets"] == ["独立实现资料解析与检索。"]
    assert old == original
    assert canonical_entry(section, result) == result


def test_native_stack_list_normalizes_without_losing_values():
    entry = Entry(organization="系统", role="公司项目", tech_stack=["Python", "SQL"])
    assert entry.tech_stack == "Python、SQL"


@pytest.mark.parametrize('template', ['t001', 't109'])
def test_project_heading_order_hides_date_without_deleting_it(tmp_path, template):
    from skill_toolbox.tools.resume_edit import ResumeEditService
    engine = ResumeEditService(tmp_path, Path('backend/skill_toolbox/skill_defs/resume_pro/templates'), template_id=template)
    entry = {'id':'p','head':{'org':'开源工具','date':'2024.01–2024.06','role':'个人项目'},
             'tech_stack':'Python', 'bullets':['完成实现'], 'details':[
                 {'label':'Stars','value':'120'}, {'label':'Forks','value':'18'},
                 {'label':'源码','value':'example/search','link':'https://github.com/example/search'}]}
    original = copy.deepcopy(entry)
    sec = {'key':'projects','title':'项目经历','entries':[entry]}
    data = engine._scenario_from_content({'sections':[sec]})['sections'][0]['entries'][0]
    assert data['text'].splitlines() == ['开源工具 · example/search · Stars：120 · Forks：18 · 个人项目',
                                      '技术栈：Python', '完成实现']
    assert entry == original
    assert '2024.01–2024.06' in engine._entry_heading({'key':'work','title':'工作经历'},entry)
