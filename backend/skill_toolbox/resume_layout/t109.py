"""t109 已认证模板的资源位置与组件映射。"""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PACKAGE_ROOT / "skill_defs/resume_pro/templates/t109/template.docx"
SPEC = {
    'page_width_pt': 595.3, 'page_height_pt': 841.9,
    'origin': {'column': 42.0},
    'sections': {key: {'title_ids': [ident], 'body': {'anchor': ident, 'shape': 1},
                       'body_offset_pt': 29.2, 'region': 'main'}
                 for key, ident in {'education': 215, 'internship': 216, 'campus': 218,
                                    'skills': 219, 'summary': 220}.items()},
    'header_columns': [{'anchor': 214, 'shape': index} for index in (2, 3)],
    'header_labels': {'姓名': 'name', '民族': 'ethnicity', '电话': 'phone', '邮箱': 'email',
                      '住址': 'address', '出生年月': 'birth', '身高': 'height',
                      '政治面貌': 'politics', '毕业院校': 'school', '学历': 'degree'},
    'photo': {'anchor': 12, 'part': 'word/media/image1.png'},
    'decorations': [213, 3],
    'regions': {'main': {'x_pt': 42.0, 'width_pt': 532.5, 'top_pt': 193.35,
                         'bottom_pt': 833.6, 'continuation_top_pt': 193.35}},
    'base_font_pt': 10.5, 'line_pitch_pt': 18.0, 'title_text_top_pt': 10.55,
    'geometry': {'title_height_pt': 30.2, 'body_offset_pt': 29.2,
                 'text_top_pt': 3.85, 'body_pad_pt': 4.6},
}
