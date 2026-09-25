"""一个 Word/WPS 测试流程覆盖六模板：生成、修改、照片比例和真实分页；DOCKIT_OFFICE_ENGINE 选择引擎。"""
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


PHOTO_SIZE, PHOTO_RGB = (300, 200), (0x35, 0x72, 0x8f)


def photo_aspect(pdf, page):
    """照片在页面上的宽高比。Word 原样嵌入照片位图；WPS 可能把相框特效与照片合成一张位图，
    此时按照片纯色像素区域测量。标题特效等其他位图不参与。"""
    import io
    infos = [i for i in page.get_image_info(xrefs=True) if not i['has-mask']]
    direct = [i for i in infos if (i['width'], i['height']) == PHOTO_SIZE]
    if direct:
        assert len(direct) == 1
        x0, y0, x1, y1 = direct[0]['bbox']
        return (x1-x0)/(y1-y0)
    boxes = []
    for info in infos:
        with Image.open(io.BytesIO(pdf.extract_image(info['xref'])['image'])) as raw:
            image = raw.convert('RGB')
        mask = image.point(lambda v: 255).convert('L')
        mask.putdata([255 if sum(abs(a-b) for a, b in zip(p, PHOTO_RGB)) < 30 else 0 for p in image.getdata()])
        if (box := mask.getbbox()) and (box[2]-box[0]) * (box[3]-box[1]) > 1000:
            boxes.append(box)
    assert len(boxes) == 1, f'照片应只出现一次，实际 {len(boxes)}'
    x0, y0, x1, y1 = boxes[0]
    return (x1-x0)/(y1-y0)


@pytest.mark.e2e
@pytest.mark.parametrize('template_id', TEMPLATE_IDS)
def test_template_generation_edit_and_pagination(tmp_path, template_id):
    pytest.importorskip('pythoncom')
    original = TEMPLATES / template_id / 'template.docx'
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    photo = tmp_path / 'portrait.png'
    Image.new('RGB', PHOTO_SIZE, '#35728f').save(photo)
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
        assert photo_aspect(pdf, pdf[0]) == pytest.approx(1.5, abs=.01)
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
