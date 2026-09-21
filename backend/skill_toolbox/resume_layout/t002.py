"""t002 简约风：源形状映射；运行时交给共享组件引擎。"""

SPEC = {
    "page_width_pt": 595.3, "page_height_pt": 841.9,
    "positions": {
        4: (38.25, 9.75), 10: (454.5, 32.25), 9: (489.75, 32.25),
        48: (525.0, 32.25), 8: (-10.05, 80.05), 38: (30.8, 108.85),
        26: (41.25, 92.2), 80: (468.9, 132.0), 37: (30.8, 236.1),
        27: (30.8, 741.25), 30: (30.8, 344.6), 29: (30.8, 540.35),
        28: (30.8, 649.5), 97: (531.0, 787.55),
    },
    "sections": {
        "education": {"title_ids": [37], "body": {"anchor": 37, "shape": 3}, "body_offset_pt": 20.918, "region": "main"},
        "internship": {"title_ids": [30], "body": {"anchor": 30, "shape": 3}, "body_offset_pt": 21.534, "region": "main"},
        "campus": {"title_ids": [29], "body": {"anchor": 29, "shape": 3}, "body_offset_pt": 21.531, "region": "main"},
        "skills": {"title_ids": [28], "body": {"anchor": 28, "shape": 3}, "body_offset_pt": 22.153, "region": "main"},
        "summary": {"title_ids": [27], "body": {"anchor": 27, "shape": 3}, "body_offset_pt": 22.153, "region": "main"},
    },
    "header_columns": [{"anchor": 38, "shape": 0}, {"anchor": 38, "shape": 1}],
    "header_labels": {
        "姓名": "name", "民族": "ethnicity", "电话": "phone", "邮箱": "email",
        "住址": "address", "出生年月": "birth", "身高": "height",
        "政治面貌": "politics", "毕业院校": "school", "学历": "degree",
    },
    "photo": {"anchor": 80, "part": "word/media/image1.png"},
    "decorations": [4, 10, 9, 48, 8, 26, 97],
    "regions": {"main": {"x_pt": 51.108, "width_pt": 506.25, "top_pt": 236.1,
                         "bottom_pt": 805.0, "continuation_top_pt": 110.0}},
    "base_font_pt": 10.0, "line_pitch_pt": 17.16, "title_text_top_pt": 0.0,
}
