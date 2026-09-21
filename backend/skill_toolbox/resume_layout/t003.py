"""t003 应届毕业生：标题文字、箭头及横线作为一个语义标题。"""

SPEC = {
    "page_width_pt": 595.3, "page_height_pt": 841.9,
    "positions": {
        31: (475.35, 80.75), 8: (236.5, 75.05), 7: (57.85, 75.05),
        9: (56.85, 224.5), 16: (60.6, 292.0), 23: (54.65, 493.1),
        26: (58.0, 627.45), 32: (-2.65, 0.35), 37: (33.0, 808.45),
        3: (-0.05, 16.15), 2: (33.8, 16.15), 36: (63.6, 724.4),
        35: (63.6, 624.0), 30: (-7.95, 851.9), 34: (63.6, 489.9),
        33: (64.35, 357.25), 29: (58.8, 726.3), 28: (25.8, 703.5),
        27: (56.0, 699.5), 25: (25.9, 603.8), 24: (56.4, 599.55),
        22: (25.9, 469.75), 21: (56.25, 465.35), 20: (58.0, 359.8),
        19: (64.35, 357.3), 18: (25.9, 336.65), 17: (56.25, 332.8),
        15: (64.35, 289.25), 14: (25.8, 268.35), 13: (56.15, 264.45),
        12: (64.35, 222.8), 11: (56.2, 198.65), 10: (25.8, 202.1),
        6: (55.8, 47.3), 4: (64.35, 71.8), 5: (25.75, 50.5),
    },
    "sections": {
        "education": {"title_ids": [11, 10, 12], "body": {"anchor": 9, "shape": None}, "body_offset_pt": 25.85, "region": "main"},
        "courses": {"title_ids": [13, 14, 15], "body": {"anchor": 16, "shape": None}, "body_offset_pt": 27.55, "region": "main"},
        "skills": {"title_ids": [17, 18, 19, 33], "body": {"anchor": 20, "shape": None}, "body_offset_pt": 27.0, "region": "main"},
        "awards": {"title_ids": [21, 22, 34], "body": {"anchor": 23, "shape": None}, "body_offset_pt": 27.75, "region": "main"},
        "work": {"title_ids": [24, 25, 35], "body": {"anchor": 26, "shape": None}, "body_offset_pt": 27.9, "region": "main"},
        "summary": {"title_ids": [27, 28, 36], "body": {"anchor": 29, "shape": None}, "body_offset_pt": 26.8, "region": "main"},
    },
    "header_columns": [{"anchor": 7, "shape": None}, {"anchor": 8, "shape": None}],
    "header_labels": {
        "姓名": "name", "民族": "ethnicity", "出生年月": "birth", "学历": "degree",
        "专业": "major", "毕业院校": "school", "联系电话": "phone", "性别": "gender",
        "籍贯": "hometown", "政治面貌": "politics", "英语水平": "english",
        "计算机水平": "computer", "现居住地": "address", "邮箱": "email",
    },
    "photo": {"anchor": 31, "part": "word/media/image1.jpeg"},
    "decorations": [32, 37, 3, 2, 30],
    "regions": {"main": {"x_pt": 58.0, "width_pt": 508.05, "top_pt": 198.65,
                         "bottom_pt": 805.0, "continuation_top_pt": 65.0}},
    "base_font_pt": 10.0, "line_pitch_pt": 16.68, "title_text_top_pt": 4.05,
}
