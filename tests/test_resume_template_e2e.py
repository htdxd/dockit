"""一个 Word 测试流程覆盖六模板：生成、修改、照片比例和真实分页。"""
import hashlib
import json

import pymupdf
import pytest
from PIL import Image
from resume_templates import TEMPLATE_IDS, TEMPLATES
from skill_toolbox.contracts.resume_workflow import EditRequest, GenerateRequest
from skill_toolbox.materials import MaterialService
from skill_toolbox.tools.materials import MaterialPlanService
from skill_toolbox.tools.resume_edit import ResumeEditService
from skill_toolbox.tools.resume_workflow import ResumeWorkflow


@pytest.mark.e2e
@pytest.mark.parametrize('template_id', TEMPLATE_IDS)
def test_template_generation_edit_and_pagination(tmp_path, template_id):
    pytest.importorskip('pythoncom')
    original = TEMPLATES / template_id / 'template.docx'
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    photo = tmp_path / 'portrait.png'
    Image.new('RGB', (300, 200), '#35728f').save(photo)
    materials = MaterialService(tmp_path)
    ir = materials.prepare_material(photo)
    engine = ResumeEditService(tmp_path, TEMPLATES, {'vision': True}, template_id=template_id)
    workflow = ResumeWorkflow(engine, MaterialPlanService(tmp_path, materials.catalog(), 'resume', {'vision': True}), template_id)
    content = {'person': {'name': '组件测试', 'phone': '13800000000', 'email': 'qa@example.com'},
        'photo_asset_id': ir.assets[0].id, 'sections': [
            {'key': 'education', 'title': '教育经历', 'entries': [{'id': 'education',
                'organization': '测试大学', 'role': '软件工程本科', 'date': '2020-2024',
                'text': ['完成软件工程课程与毕业设计。'], 'source_ids': ['fixture']}]},
            {'key': 'projects', 'title': '项目经历', 'entries': [{'id': 'project',
                'organization': '检索工具', 'role': '个人项目', 'tech_stack': 'Python、TypeScript',
                'text': ['实现数据索引，检索耗时降低 30%。'], 'source_ids': ['fixture'],
                'highlights': [{'field': 'text', 'text': '降低 30%', 'emphasis': 'bold_accent'}]}]},
            {'key': 'summary', 'title': '个人评价', 'entries': [{'id': 'summary',
                'text': ['能够独立完成需求分析与交付验证。'], 'source_ids': ['fixture']}]},
        ]}
    workflow.facts.add('fixture', json.dumps(content, ensure_ascii=False), kind='request')
    workflow.facts.save()
    first = workflow.generate(GenerateRequest(content=content))
    assert first.ok, first.as_dict()
    first_record = engine.store.load_revision(first.artifact_id, first.revision)
    assert first_record['mechanical']['passed'] and first_record['page_count'] == 1
    with pymupdf.open(tmp_path / first_record['pdf']) as pdf:
        # Word 可能把照片阴影/标题效果也导出为带 mask 的图；原照片是不透明位图。
        images = [image for image in pdf[0].get_image_info() if not image['has-mask']]
        assert len(images) == 1
        x0, y0, x1, y1 = images[0]['bbox']
        assert (x1-x0)/(y1-y0) == pytest.approx(1.5, abs=.01)
    changes = [{'op': 'insert_entry', 'section_id': 'projects', 'entry': {
        'id': f'additional-{n}', 'organization': f'扩展项目{n}', 'role': '独立负责',
        'text': [f'第{k}项：完成需求分析、实现和回归测试。' for k in range(8)], 'source_ids': ['fixture']}}
        for n in range(4)]
    changes.append({'op': 'format', 'scope': 'entry', 'target_id': 'projects#project', 'font_size_pt': 11})
    edited = workflow.edit(EditRequest(candidate_id=first.data['candidate_id'], changes=changes, target_pages=3))
    assert edited.ok, edited.as_dict()
    final = engine.store.load_revision(edited.artifact_id, edited.revision)
    assert final['mechanical']['passed'] and 2 <= final['page_count'] <= 3
    assert len(final['content']['sections'][1]['entries']) == 5
    assert final['content']['sections'][1]['entries'][0]['font_size_pt'] == 11
    with pymupdf.open(tmp_path / final['pdf']) as pdf:
        spans = [span for page in pdf for block in page.get_text('dict')['blocks']
                 for line in block.get('lines', []) for span in line['spans'] if '检索耗时' in span['text']]
        assert spans and all(span['size'] == pytest.approx(11, abs=.1) for span in spans)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
