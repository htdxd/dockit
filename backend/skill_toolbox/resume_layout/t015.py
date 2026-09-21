"""t015 行政管理：经历条目的岗位日期与组织正文分列映射。"""

SPEC = {
    "page_width_pt": 595.3, "page_height_pt": 841.9,
    "positions": {
        56: (41.3, 106.2), 7: (23.2, 106.0), 59: (23.2, 698.55),
        60: (23.2, 459.45), 61: (23.2, 345.75), 13: (242.1, 186.45),
        12: (242.1, 203.9), 11: (241.6, 169.8), 9: (243.35, 147.55),
        15: (31.5, 141.0), 16: (261.75, 141.15), 23: (31.5, 268.45),
        32: (31.05, 382.25), 47: (30.55, 625.75), 41: (30.55, 497.35),
        44: (30.55, 555.0), 54: (31.35, 737.55), 17: (478.2, 110.55),
        62: (23.2, 229.65), 55: (238.05, 25.6), 1: (-2.8, -1.3),
        10: (221.7, 63.7), 8: (23.2, 24.9),
    },
    "sections": {
        "summary": {"title_ids": [62], "body": {"anchor": 23, "shape": None}, "body_offset_pt": 38.8, "region": "main"},
        "education": {"title_ids": [61], "body": {"anchor": 32, "shape": 0}, "side_body": {"anchor": 32, "shape": 1}, "body_offset_pt": 36.5, "region": "main"},
        "work": {"title_ids": [60], "body": {"anchor": 41, "shape": 0}, "side_body": {"anchor": 41, "shape": 1}, "body_offset_pt": 37.9, "region": "main"},
        "skills": {"title_ids": [59], "body": {"anchor": 54, "shape": None}, "body_offset_pt": 39.0, "region": "main"},
    },
    "header_columns": [{"anchor": 15, "shape": None}, {"anchor": 16, "shape": None}],
    "header_labels": {
        "姓名": "name", "年龄": "age", "籍贯": "hometown", "求职意向": "target_role",
        "手机": "phone", "邮箱": "email", "微信": "wechat", "微博": "weibo",
    },
    "photo": {"anchor": 17, "part": "word/media/image1.jpeg"},
    "decorations": [1, 8, 55, 10],
    "extra_remove_ids": [44, 47, 9, 11, 13, 12],
    "regions": {"main": {"x_pt": 31.05, "width_pt": 523.1, "top_pt": 229.65,
                         "bottom_pt": 805.0, "continuation_top_pt": 110.0}},
    "base_font_pt": 10.5, "line_pitch_pt": 18.0, "title_text_top_pt": 4.05,
}
