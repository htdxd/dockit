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
        result[key] = {"label": label.strip(), "before": originals.get(key, ""),
                       "after": value, "column": column}
    # 空文本框仍保留合法空段落，不保留已删除的标签或值。
    for tx in contents:
        if not tx.findall(emit.W + "p"):
            emit.etree.SubElement(tx, emit.W + "p")
    return result


def has_header_edits(header: dict) -> bool:
    return any(header.get(key) for key in ("fields", "hidden_fields", "custom_fields"))
