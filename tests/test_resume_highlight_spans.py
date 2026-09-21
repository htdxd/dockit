from copy import deepcopy
from pathlib import Path

import pytest
from lxml import etree

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.resume_layout.field_style import W, apply_text_spans
from skill_toolbox.tools.resume_edit import ResumeEditService


def test_phrase_across_runs_keeps_tabs_links_and_text():
    p = etree.fromstring(f'<w:p xmlns:w="{W[1:-1]}"><w:r><w:t>负责</w:t><w:tab/></w:r><w:fldSimple w:instr=" HYPERLINK &quot;https://example.com&quot; "><w:r><w:t>自动</w:t></w:r><w:r><w:t>化部署</w:t></w:r></w:fldSimple></w:p>')
    apply_text_spans(p, [{'start':3,'end':8,'emphasis':'bold_accent','background':True}])
    assert ''.join(p.itertext()) == '负责自动化部署'
    assert len(list(p.iter(W+'tab'))) == 1
    assert len(list(p.iter(W+'fldSimple'))) == 1
    for run in p.find(W+'fldSimple').iter(W+'r'):
        assert run.find(W+'rPr/'+W+'b') is not None
        assert run.find(W+'rPr/'+W+'shd') is not None


def test_title_tab_stops_do_not_count_as_visible_characters():
    from skill_toolbox.resume_layout.field_style import apply_inline_styles
    p = etree.fromstring(f'<w:p xmlns:w="{W[1:-1]}"><w:pPr><w:tabs><w:tab w:pos="100"/><w:tab w:pos="200"/></w:tabs></w:pPr><w:r><w:t>项目</w:t><w:tab/><w:t>Stars：120 ｜ 个人项目</w:t></w:r></w:p>')
    apply_inline_styles(p,[{'text':'Stars：120','emphasis':'bold'}])
    bold = ''.join(t.text or '' for r in p.iter(W+'r') if r.find(W+'rPr/'+W+'b') is not None for t in r.iter(W+'t'))
    assert bold == 'Stars：120'


@pytest.mark.parametrize('template', ['t001','t109'])
def test_semantic_targets_and_inline_metrics(tmp_path, template):
    engine = ResumeEditService(tmp_path, Path('backend/skill_toolbox/skill_defs/resume_pro/templates'), template_id=template)
    entry = {'id':'p1','head':{'org':'检索系统','role':'个人项目'},'tech_stack':'Python',
             'bullets':['完成自动化部署，补充文档。'],
             'details':[{'label':'下载量','value':'120','layout':'inline','emphasis':'bold'},
                        {'label':'用户','value':'30','layout':'inline','emphasis':'accent'}],
             'highlights':[{'field':'text','paragraph':0,'text':'自动化部署','emphasis':'bold','background':True},
                           {'field':'organization','emphasis':'accent'}]}
    original = deepcopy(entry)
    section = {'key':'projects','title':'项目经历','entries':[entry]}
    scenario = engine._scenario_from_content({'sections':[section]})['sections'][0]['entries'][0]
    lines = scenario['text'].splitlines()
    assert lines[2] == '下载量：120 · 用户：30'
    assert lines[3] == entry['bullets'][0]
    assert scenario['paragraph_styles'][3][0]['start'] == 2
    assert entry == original
    entry['bullets'] = ['已修改原文']
    with pytest.raises(ToolError, match='未唯一匹配'):
        engine._scenario_from_content({'sections':[section]})
    entry['highlights'] = []
    assert engine._scenario_from_content({'sections':[section]})


def test_duplicate_fragment_is_rejected():
    from skill_toolbox.resume_layout.highlights import compile_highlights
    entry = {'id':'p','bullets':['测试与测试'], 'highlights':[{'field':'text','text':'测试'}]}
    with pytest.raises(ToolError, match='未唯一匹配'):
        compile_highlights(entry,'',('org','role','date'),0)


@pytest.mark.parametrize('template', ['t001','t109'])
@pytest.mark.parametrize('stack', ['', 'Python'])
def test_github_short_label_uses_existing_line_and_keeps_destination(tmp_path, template, stack):
    engine = ResumeEditService(tmp_path, Path('backend/skill_toolbox/skill_defs/resume_pro/templates'), template_id=template)
    url = 'https://github.com/example/search/tree/main/docs'
    entry = {'id':'p','head':{'org':'检索工具','role':'个人项目'},'tech_stack':stack,
             'details':[{'label':'源码','value':url,'link':url}], 'bullets':['实现检索。']}
    data = engine._scenario_from_content({'sections':[{'key':'projects','title':'项目经历','entries':[entry]}]})['sections'][0]['entries'][0]
    lines = data['text'].splitlines()
    assert len(lines) == (3 if stack else 2)
    assert 'example/search' in lines[0]
    assert 'https://' not in data['text']
    styles = [s for spans in data['paragraph_styles'].values() for s in spans] + data['inline_styles']
    assert any(s.get('link') == url for s in styles)
    assert entry['details'][0]['value'] == url
