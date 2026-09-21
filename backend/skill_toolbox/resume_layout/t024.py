"""t024 双栏模板的源形状映射；坐标经 Microsoft Word 实测。"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "skill_defs/resume_pro/templates/t024/template.docx"
PHOTO_ID = 46
PHOTO_PART = "word/media/image1.jpeg"
DECORATIONS = (1, 2)
NAME_ID = 3
PERSONAL_TITLE_ID, PERSONAL_BODY_ID = 39, 45
CONTACT_TITLE_ID, CONTACT_BODY_ID = 44, 4

# 栏目标题组 docPr.id、独立正文 docPr.id、正文相对标题的 y 偏移。
SECTIONS = {
    "education": (14, 6, 22.25),
    "work": (19, 5, 22.90),
    "awards": (24, 8, 22.20),
    "skills": (29, 7, 28.45),
    "summary": (34, 9, 35.20),
}
SIDEBAR_SECTIONS = frozenset({"summary"})
PAGE_WIDTH_PT, PAGE_HEIGHT_PT = 592.5, 839.15
PAGE_BOTTOM_PT = 815.0
MAIN_TOP_PT, SIDEBAR_TOP_PT = 37.8, 553.1
MAIN_X_PT, MAIN_WIDTH_PT = 196.9, 365.05
SIDEBAR_X_PT, SIDEBAR_WIDTH_PT = 16.45, 147.8
HEADER_LABELS = {
    "姓名": "name", "应聘岗位": "target_role", "籍贯": "hometown",
    "出生年月": "birth_date", "政治面貌": "political_status",
    "现居居地": "address", "电话": "phone", "邮箱": "email",
}

# 原件每个 anchor 挂在不同段落，原始 posV 不能统一加固定页边距。
# 这里冻结 Word COM Anchor.Information(6) + Shape.Top 的页面绝对值。
PAGE_POSITIONS = {
    1: (-0.65, 0.2), 2: (7.35, 6.45), 14: (210.3, 37.8),
    46: (66.35, 52.35), 6: (195.2, 60.05), 19: (210.1, 165.2),
    5: (196.9, 188.1), 3: (44.8, 194.8), 39: (24.9, 278.35),
    45: (-4.45, 301.2), 44: (27.45, 425.3), 4: (-6.25, 450.2),
    34: (28.3, 553.1), 24: (209.55, 550.9), 8: (195.5, 573.1),
    9: (16.45, 588.3), 29: (209.65, 677.5), 7: (214.1, 705.95),
}

SPEC = {
    "page_width_pt": PAGE_WIDTH_PT,
    "page_height_pt": PAGE_HEIGHT_PT,
    "positions": PAGE_POSITIONS,
    "sections": {
        key: {
            "title_ids": [title],
            "body": {"anchor": body, "shape": None},
            "body_offset_pt": offset,
            "region": "sidebar" if key in SIDEBAR_SECTIONS else "main",
            "line_pitch_pt": 13.8 if key in {"summary", "skills"} else 18.0,
        }
        for key, (title, body, offset) in SECTIONS.items()
    },
    "header_columns": [
        {"anchor": PERSONAL_BODY_ID, "shape": None},
        {"anchor": CONTACT_BODY_ID, "shape": None},
    ],
    "header_plain": {
        "name": {"anchor": NAME_ID, "shape": None, "paragraph": 0},
        "target_role": {"anchor": NAME_ID, "shape": None, "paragraph": 1},
    },
    "header_labels": HEADER_LABELS,
    "header_unlabelled": {
        "phone": {"anchor": CONTACT_BODY_ID, "paragraph": 0, "label": "电话"},
        "email": {"anchor": CONTACT_BODY_ID, "paragraph": 1, "label": "邮箱"},
    },
    "header_groups": [
        {"title_ids": [PERSONAL_TITLE_ID], "columns": [PERSONAL_BODY_ID]},
        {"title_ids": [CONTACT_TITLE_ID], "columns": [CONTACT_BODY_ID]},
    ],
    "photo": {"anchor": PHOTO_ID, "part": PHOTO_PART},
    "decorations": list(DECORATIONS),
    "regions": {
        "main": {
            "x_pt": MAIN_X_PT, "width_pt": MAIN_WIDTH_PT,
            "top_pt": MAIN_TOP_PT, "bottom_pt": PAGE_BOTTOM_PT,
            "continuation_top_pt": MAIN_TOP_PT,
        },
        "sidebar": {
            "x_pt": SIDEBAR_X_PT, "width_pt": SIDEBAR_WIDTH_PT,
            "top_pt": SIDEBAR_TOP_PT, "bottom_pt": PAGE_BOTTOM_PT,
            "continuation_top_pt": MAIN_TOP_PT,
        },
    },
    "base_font_pt": 10.0,
    "line_pitch_pt": 18.0,
    "title_text_top_pt": 0.65,
}
