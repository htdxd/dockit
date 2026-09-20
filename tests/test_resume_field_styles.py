from pathlib import Path
import pytest
from lxml import etree

from skill_toolbox.resume_layout import emit
from skill_toolbox.resume_layout.header import apply_header_components


def test_custom_header_field_preserves_link_bold_and_theme():
    box = etree.fromstring(f'<x xmlns:w="{emit.W[1:-1]}"><w:txbxContent><w:p><w:r><w:t>姓名：张三</w:t></w:r></w:p></w:txbxContent></x>')
    apply_header_components([box], {'姓名':'name'}, {'custom_fields':[
        {'key':'github','label':'GitHub','value':'user/project','link':'https://github.com/user/project','emphasis':'bold_accent'}]})
    field = box.find('.//' + emit.W + 'fldSimple')
    assert 'https://github.com/user/project' in field.get(emit.W+'instr')
    assert field.find('.//'+emit.W+'b').get(emit.W+'val') == '1'
    assert field.find('.//'+emit.W+'color') is not None
    assert 'user/project' in ''.join(box.itertext())


def test_details_are_non_bulleted_and_styled_before_measurement():
    root = emit.load_document_xml(Path('backend/skill_toolbox/skill_defs/resume_pro/templates/t109/template.docx'))
    body = emit.find_body_wsp(emit._anchor_by_docpr_name(root,'组合 216'))
    emit.set_box_text(body,'项目\n技术栈：Python\nStars：120\n实现搜索功能',header_lines=1,tech_stack_line=1,
                      detail_styles={2:{'emphasis':'bold_accent','link':'https://github.com/user/project'}})
    ps = list(body.iter(emit.W+'p'))
    assert ps[2].find('.//'+emit.W+'numPr') is None
    assert ps[2].find('.//'+emit.W+'b') is not None
    assert ps[3].find('.//'+emit.W+'numPr') is not None


def test_accent_removes_template_text_fill_that_overrides_color():
    from skill_toolbox.resume_layout.field_style import apply_field_style
    p = etree.fromstring(f'<w:p xmlns:w="{emit.W[1:-1]}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:r><w:rPr><w14:textFill/></w:rPr><w:t>Stars 120</w:t></w:r></w:p>')
    apply_field_style(p, {'emphasis':'bold_accent'})
    assert not p.findall('.//{http://schemas.microsoft.com/office/word/2010/wordml}textFill')
    assert p.find('.//'+emit.W+'color').get(emit.W+'val') == '244761'


def test_custom_field_schema_rejects_unsafe_link():
    from pydantic import ValidationError
    from skill_toolbox.contracts.resume_style import DetailField
    assert DetailField(label='Stars',value=120).value == '120'
    with pytest.raises(ValidationError):
        DetailField(label='作品',value='查看',link='javascript:alert(1)')


@pytest.mark.parametrize('template',['t001','t109'])
def test_project_metrics_share_title_before_project_kind(tmp_path, template):
    from skill_toolbox.tools.resume_edit import ResumeEditService
    engine = ResumeEditService(tmp_path,Path('backend/skill_toolbox/skill_defs/resume_pro/templates'),template_id=template)
    scenario = engine._scenario_from_content({'sections':[{'key':'projects','title':'项目经历','entries':[
        {'id':'project-1','head':{'org':'工具项目','role':'个人项目'},'tech_stack':'Python',
         'details':[{'label':'Stars','value':'120','emphasis':'bold_accent'}, {'label':'Forks','value':'18','emphasis':'bold'}],
         'bullets':['编写文档']}]}]})
    entry = scenario['sections'][0]['entries'][0]
    lines = entry['text'].splitlines()
    assert len(lines) == 3
    assert lines[0].index('工具项目') < lines[0].index('Stars：120') < lines[0].index('Forks：18') < lines[0].index('个人项目')
    assert lines[1] == '技术栈：Python'
    assert not entry['detail_styles'] and len(entry['inline_styles']) == 2


def test_inline_emphasis_preserves_title_text_tabs_and_order():
    from skill_toolbox.resume_layout.field_style import apply_inline_styles
    p = etree.fromstring(f'<w:p xmlns:w="{emit.W[1:-1]}"><w:r><w:t>项目</w:t><w:tab/><w:t>Stars：120 · Forks：18 ｜ 个人项目</w:t></w:r></w:p>')
    apply_inline_styles(p,[{'text':'Stars：120','emphasis':'bold_accent'},{'text':'Forks：18','emphasis':'bold'}])
    assert ''.join(t.text or '' for t in p.iter(emit.W+'t')) == '项目Stars：120 · Forks：18 ｜ 个人项目'
    assert len(p.findall('.//'+emit.W+'tab')) == 1
    stars = next(t.getparent() for t in p.iter(emit.W+'t') if t.text == 'Stars：120')
    assert stars.find('.//'+emit.W+'color') is not None
    assert stars.find('.//'+emit.W+'b') is not None
