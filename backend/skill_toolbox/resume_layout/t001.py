"""t001 的栏目、头部和几何定义；行为由共享组件引擎实现。"""
# docPr.id 唯一；原件多个独立正文框具有同名 Rectangle 3。
SECTIONS = {
    "summary": (40, 21, 25.2), "education": (11, 18, 26.25),
    "work": (12, 20, 29.4), "skills": (35, 19, 26.2),
}
DECORATIONS = (14, 7, 15, 3, 33)

SPEC = {
    'page_width_pt': 595.3, 'page_height_pt': 841.9,
    'origin': {'column': 90.0, 'paragraph': 72.0},
    'sections': {key: {'title_ids': [title], 'body': {'anchor': body, 'shape': None},
                       'body_offset_pt': offset, 'region': 'main'}
                 for key, (title, body, offset) in SECTIONS.items()},
    'header_columns': [{'anchor': ident, 'shape': None} for ident in (22, 62)],
    'header_labels': {'姓名': 'name', '年龄': 'age', '学历': 'degree', '求职意向': 'target_role',
                      '手机': 'phone', '邮箱': 'email', '微信': 'wechat', '地址': 'address'},
    'header_slot_order': ('role', 'org', 'date'),
    'two_slot_tab': 'right', 'title_font_pt': 12.0,
    'photo': {'anchor': 5, 'part': 'word/media/image1.jpeg'},
    'decorations': list(DECORATIONS), 'extra_remove_ids': [8],
    'regions': {'main': {'x_pt': 57.6, 'width_pt': 506.75, 'top_pt': 243.7,
                         'bottom_pt': 815.0, 'continuation_top_pt': 110.0}},
    'base_font_pt': 10.0, 'line_pitch_pt': 18.0, 'title_text_top_pt': 3.9,
    'geometry': {'title_height_pt': 23.2, 'body_offset_pt': 29.4,
                 'text_top_pt': 4.05, 'body_pad_pt': 4.0},
}
