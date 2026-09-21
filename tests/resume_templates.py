"""模板测试共同入口；与生产一样按 SPEC 选取组件，不另写模板识别规则。"""
from pathlib import Path

from skill_toolbox.resume_layout import component_template as ct
from skill_toolbox.resume_layout import emit

TEMPLATES = Path(__file__).resolve().parents[1] / 'backend/skill_toolbox/skill_defs/resume_pro/templates'
TEMPLATE_IDS = sorted(ct.TEMPLATE_IDS)


def body_for(template, section='work'):
    spec = ct.get_spec(template.parent.name)
    root = ct.load_normalized(template, spec)
    ref = spec['sections'][ct.source_key({'id': section}, spec)]['body']
    return next(ct.box_anchor(root, ref).iter(emit.WPS + 'wsp'))


def header_boxes(template_id):
    spec = ct.get_spec(template_id)
    root = ct.load_normalized(TEMPLATES / template_id / 'template.docx', spec)
    return [next(ct.box_anchor(root, ref).iter(emit.WPS + 'wsp')) for ref in spec['header_columns']]
