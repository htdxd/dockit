"""个人信息行组件：原位改名、移除整行，以及沿用模板样式添加新行。"""
from __future__ import annotations

import copy

from skill_toolbox.resume_layout import emit


def _paragraph_value(paragraph) -> tuple[str, str] | None:
    text = "".join(t.text or "" for t in paragraph.iter(emit.W + "t"))
    return emit._split_label_value(text)


def _set_label_value(paragraph, label: str, value: str):
    # 长网址可能自然换行；新标签不能沿用两端对齐而把冒号拉到列末。
    ppr = paragraph.find(emit.W + "pPr")
    if ppr is None:
        ppr = emit.etree.Element(emit.W + "pPr")
        paragraph.insert(0, ppr)
    alignment = ppr.find(emit.W + "jc")
    if alignment is None:
        alignment = emit.etree.SubElement(ppr, emit.W + "jc")
    alignment.set(emit.W + "val", "left")
    word_wrap = ppr.find(emit.W + "wordWrap")
    if word_wrap is None:
        word_wrap = emit.etree.SubElement(ppr, emit.W + "wordWrap")
    word_wrap.set(emit.W + "val", "0")
    runs = list(paragraph.iter(emit.W + "r"))
    sample = copy.deepcopy(runs[-1]) if runs else emit.etree.Element(emit.W + "r")
    for run in runs:
        run.getparent().remove(run)
    for text in (label + "：", value):
        run = copy.deepcopy(sample)
        emit._set_run_text(run, text)
        paragraph.append(run)


def apply_header_components(boxes: list, labels: dict[str, str], header: dict) -> dict:
    """custom_fields 是完整覆盖列表；hidden_fields 对基准行与自定义行均优先。"""
    fields = header.get("fields") or {}
    hidden = set(header.get("hidden_fields") or [])
    custom = {item["key"]: item for item in header.get("custom_fields") or []}
    contents = [box.find(".//" + emit.W + "txbxContent") for box in boxes]
    samples = [copy.deepcopy(next(tx.iter(emit.W + "p"))) for tx in contents]
    originals, rows = {}, {}
    for column, (box, tx) in enumerate(zip(boxes, contents)):
        for p in list(tx.iter(emit.W + "p")):
            pair = _paragraph_value(p)
            if pair is None:
                continue
            label, value = pair
            key = labels.get("".join(label.split()))
            if key is None:
                continue
            originals[key] = value
            if key in hidden:
                p.getparent().remove(p)
            else:
                rows[key] = (column, p)
        emit.set_info_fields(box, fields, labels=labels)
    for key, field in custom.items():
        if key in hidden:
            continue
        if key in rows:
            column, paragraph = rows[key]
        else:
            column = min(range(len(contents)), key=lambda i: len(contents[i].findall(emit.W + "p")))
            paragraph = copy.deepcopy(samples[column])
            contents[column].append(paragraph)
            rows[key] = (column, paragraph)
        _set_label_value(paragraph, field["label"], field["value"])
    result = {}
    for key, (column, paragraph) in rows.items():
        label, value = _paragraph_value(paragraph)
        _set_label_value(paragraph, "".join(label.split()), value.strip())
        # 每列采用统一标签宽度；续行与值对齐，清除原模板残留制表符/缩进。
        ppr = paragraph.find(emit.W + "pPr")
        for tag in ("tabs", "ind"):
            for child in ppr.findall(emit.W + tag):
                ppr.remove(child)
        label_length = max(len("".join(_paragraph_value(p)[0].split())) for c, p in rows.values() if c == column)
        font_half_points = max((int(sz.get(emit.W + "val")) for c, p in rows.values() if c == column
                               for sz in p.iter(emit.W + "sz")), default=22)
        width = (label_length + 1) * font_half_points * 10 + 120
        indent = emit.etree.SubElement(ppr, emit.W + "ind")
        indent.set(emit.W + "left", str(width))
        indent.set(emit.W + "hanging", str(width))
        tabs = emit.etree.SubElement(ppr, emit.W + "tabs")
        tab = emit.etree.SubElement(tabs, emit.W + "tab")
        tab.set(emit.W + "val", "left")
        tab.set(emit.W + "pos", str(width))
        runs = list(paragraph.iter(emit.W + "r"))
        for run in runs:
            for old in list(run.findall(emit.W + "tab")):
                run.remove(old)
        emit.etree.SubElement(runs[0], emit.W + "tab")
        result[key] = {"label": label.strip(), "before": originals.get(key, ""),
                       "after": value, "column": column}
    # 空文本框仍保留合法空段落，不保留已删除的标签或值。
    for tx in contents:
        if not tx.findall(emit.W + "p"):
            emit.etree.SubElement(tx, emit.W + "p")
    return result


def has_header_edits(header: dict) -> bool:
    return any(header.get(key) for key in ("fields", "hidden_fields", "custom_fields"))


def check_rendered_alignment(pdf_path, components: dict, tolerance: float = 1.0) -> list:
    """所有模板共用的渲染检查：标签、值及续行分别对齐，使用 PDF 真实字符坐标。"""
    import pymupdf
    from skill_toolbox.resume_layout.qa import QAIssue

    issues, columns = [], {}
    with pymupdf.open(pdf_path) as pdf:
        chars = [char for block in pdf[0].get_text("rawdict")["blocks"]
                 for line in block.get("lines", []) for span in line["spans"]
                 for char in span["chars"] if not char["c"].isspace()]
    normalize = lambda text: "".join(text.split()).replace(":", "：")
    stream = normalize("".join(char["c"] for char in chars))
    for key, field in components.items():
        label, value = normalize(field["label"]) + "：", normalize(field["after"])
        if not value:
            continue
        start = stream.find(label + value)
        if start < 0 or "column" not in field:
            issues.append(QAIssue("header_alignment", "error", f"个人信息 {key} 缺少完整渲染文字或列信息，无法验收对齐"))
            continue
        label_char, value_char = chars[start], chars[start + len(label)]
        lx, ly = label_char["origin"]
        vx, vy = value_char["origin"]
        columns.setdefault(field["column"], []).append((key, lx, vx))
        if abs(ly - vy) > tolerance:
            issues.append(QAIssue("header_alignment", "error", f"个人信息 {key} 的标签和值不在同一行"))
        previous_y = vy
        for char in chars[start + len(label):start + len(label) + len(value)]:
            x, y = char["origin"]
            if abs(y - previous_y) > tolerance and abs(x - vx) > tolerance:
                issues.append(QAIssue("header_alignment", "error", f"个人信息 {key} 续行未与内容列对齐"))
                break
            previous_y = y
    for rows in columns.values():
        for index, name in ((1, "标签"), (2, "内容")):
            if max(row[index] for row in rows) - min(row[index] for row in rows) > tolerance:
                issues.append(QAIssue("header_alignment", "error", f"个人信息同列{name}起点不齐：" + "、".join(row[0] for row in rows)))
    return issues


def rendered_field_bounds(page, components: dict) -> list:
    """按完整字段定位 PDF 文字，供头部布局共享使用。"""
    chars = [c for b in page.get_text("rawdict")["blocks"] for line in b.get("lines", [])
             for span in line["spans"] for c in span["chars"] if not c["c"].isspace()]
    normalize = lambda s: "".join(s.split()).replace(":", "：")
    stream = normalize("".join(c["c"] for c in chars))
    bounds = []
    for field in components.values():
        needle = normalize(field["label"] + "：" + field["after"])
        start = stream.find(needle)
        if start < 0:
            raise ValueError("HEADER_CONTENT_MISSING: 无法定位个人信息真实底部")
        bounds.extend(c["bbox"] for c in chars[start:start + len(needle)])
    return bounds


def body_start_from_render(pdf_path, components: dict, original_top: float, gap: float = 8.0) -> float:
    """正文从信息文字与上方照片/装饰的真实边界之后开始，不用文本框空白高度。"""
    import pymupdf

    with pymupdf.open(pdf_path) as pdf:
        page = pdf[0]
        bottoms = [box[3] for box in rendered_field_bounds(page, components)]
        for info in page.get_image_info():
            box = pymupdf.Rect(info["bbox"])
            if box.y0 < original_top and box.y1 < original_top:
                bottoms.append(box.y1)
        for drawing in page.get_drawings():
            box = drawing["rect"]
            if box.y0 < original_top and box.y1 < original_top:
                bottoms.append(box.y1 + (drawing.get("width") or 0) / 2)
        return round(max(bottoms, default=original_top - gap) + gap, 2)
