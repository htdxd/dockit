"""t109 已认证模板的资源位置与组件映射。"""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PACKAGE_ROOT / "skill_defs/resume_pro/templates/t109/template.docx"
RENDER_SCRIPT = PACKAGE_ROOT / "skill_defs/resume_pro/scripts/render_pages.py"

# 原件栏目 anchor 的 docPr name（P0 解析）：克隆源
SOURCE_SECTION_NAMES = {
    "education": "组合 215",
    "internship": "组合 216",
    "campus": "组合 218",
    "skills": "组合 219",
    "summary": "组合 220",
}
# Word COM Shapes 序号（宿主文档内，1-based；P0 探针验证）：
# 1=顶带 2=照片 3=教育 4=实习 5=校园 6=技能 7=自我 8=个人信息 9=底条
# 逐栏目测量：条目在「本栏目自己的正文框」里测（样式/项目符号影响 wrap）
# 母版 anchor 的 docPr name（个人信息区 / 照片）：header.fields 与照片替换用
MASTER_INFO_ANCHOR_NAME = "组合 214"
MASTER_PHOTO_ANCHOR_NAME = "组合 12"
MEASURE_SHAPE_BY_SECTION = {
    "education": 3, "internship": 4, "campus": 5,
    "skills": 6, "summary": 7,
}
MEASURE_BODY_ITEM_INDEX = 2        # GroupItems(2)=正文框
# 新增栏目（模板中不存在）复用实习栏原型（同为经历型：头段+职责段带符号）
FALLBACK_PROTO_SECTION = "internship"


def header_boxes(root) -> list:
    from skill_toolbox.resume_layout import emit
    anchor = emit._anchor_by_docpr_name(root, MASTER_INFO_ANCHOR_NAME)
    boxes = []
    for box in anchor.iter(emit.WPS + "wsp"):
        for p in box.iter(emit.W + "p"):
            pair = emit._split_label_value("".join(t.text or "" for t in p.iter(emit.W + "t")))
            if pair and "".join(pair[0].split()) in emit.INFO_FIELD_LABELS:
                boxes.append(box)
                break
    return boxes


def photo_top_for_header(text_bottom: float, height: float, band_bottom: float,
                         effect_top: float, effect_bottom: float) -> float:
    """照片外框底部贴合信息区，顶部与页眉色带至少留 8pt。"""
    return round(max(band_bottom + 8 + effect_top,
                     text_bottom - height - effect_bottom), 2)


def fit_header(template: Path, header: dict, work_dir: Path) -> dict:
    """t109 组内两列沿用原框；增删字段后用 Word 实际检查容量。"""
    from skill_toolbox.resume_layout import emit
    from skill_toolbox.resume_layout.header import apply_header_components

    root = emit.load_document_xml(template)
    anchor = emit._anchor_by_docpr_name(root, MASTER_INFO_ANCHOR_NAME)
    boxes = header_boxes(root)
    anchor.find(emit.WP + "docPr").set("name", "ResumeHeaderGroup")
    for index, box in enumerate(boxes):
        for child in box.iter():
            if emit.etree.QName(child).localname == "cNvPr":
                child.set("name", f"ResumeHeader{index}")
    records = apply_header_components(boxes, emit.INFO_FIELD_LABELS, header)
    host = work_dir / "header_host.docx"
    emit.save_document_xml(root, template, host)

    def find_child(group, name):
        for i in range(1, group.GroupItems.Count + 1):
            child = group.GroupItems(i)
            if child.Name == name:
                return child
            if child.Type == 6:
                found = find_child(child, name)
                if found is not None:
                    return found
        return None

    from skill_toolbox.word_com import start_word, close_word
    word = start_word()
    doc = None
    try:
        doc = word.Documents.Open(str(host.resolve()), False, True)
        group = doc.Shapes("ResumeHeaderGroup")
        result = {}
        for index in range(len(boxes)):
            shape = find_child(group, f"ResumeHeader{index}")
            if shape is None:
                raise ValueError(f"HEADER_UNKNOWN: t109 个人信息列 {index + 1} 未找到")
            if shape.TextFrame.Overflowing:
                raise ValueError(f"HEADER_OVERFLOW: t109 个人信息第 {index + 1} 列在原字号/原框内放不下")
            # t109 的 spAutoFit 可能自动增高，Overflowing=False 并不代表仍在母版区域。
            text_top = float(shape.TextFrame.TextRange.Characters(1).Information(6))
            bottom = text_top - 3.85 + float(shape.Height)
            if bottom > 193.35 - 2.0:
                raise ValueError(f"HEADER_OVERFLOW: t109 个人信息第 {index + 1} 列增高后侵入正文区域")
            result[str(index)] = {"width_pt": round(shape.Width, 2),
                                  "height_pt": round(shape.Height, 2), "bottom_pt": round(bottom, 2),
                                  "overflow": False}
        if records and not header.get("hide_photo"):
            import pymupdf
            from skill_toolbox.resume_layout.header import rendered_field_bounds
            pdf_path = work_dir / "header_host.pdf"
            doc.ExportAsFixedFormat(str(pdf_path.resolve()), 17)
            with pymupdf.open(pdf_path) as pdf:
                page = pdf[0]
                text_bottom = max(b[3] for b in rendered_field_bounds(page, records))
                band_bottom = max(d["rect"].y1 for d in page.get_drawings()
                                  if d["rect"].width > page.rect.width * 0.8
                                  and d["rect"].y1 < 60)
            photo = emit._anchor_by_docpr_name(root, MASTER_PHOTO_ANCHOR_NAME)
            effect = photo.find(emit.WP + "effectExtent")
            result["photo_y_pt"] = photo_top_for_header(
                text_bottom, emit.emu2pt(photo.find(emit.WP + "extent").get("cy")),
                band_bottom, emit.emu2pt(effect.get("t")), emit.emu2pt(effect.get("b")))
        return result
    finally:
        close_word(word, doc)
