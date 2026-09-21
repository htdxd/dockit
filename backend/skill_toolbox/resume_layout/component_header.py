"""共享头部回填与 Word 实测；测量和输出使用同一组件构造过程。"""
from __future__ import annotations

import copy
import zipfile
from pathlib import Path

from skill_toolbox.resume_layout import emit
from skill_toolbox.resume_layout.field_style import apply_field_style
from skill_toolbox.resume_layout.header import _set_label_value, apply_header_components


def _geometry(anchor) -> dict:
    return {
        "x_pt": int(anchor.find(emit.WP + "positionH/" + emit.WP + "posOffset").text) / emit.EMU,
        "y_pt": int(anchor.find(emit.WP + "positionV/" + emit.WP + "posOffset").text) / emit.EMU,
        "width_pt": int(anchor.find(emit.WP + "extent").get("cx")) / emit.EMU,
        "height_pt": int(anchor.find(emit.WP + "extent").get("cy")) / emit.EMU,
    }


def _body(anchor):
    return next(anchor.iter(emit.WPS + "wsp"))


def _rewrite(root, spec: dict, header: dict):
    from skill_toolbox.resume_layout import component_template as ct

    refs = spec.get("header_columns", [])
    columns = [ct.box_anchor(root, ref) for ref in refs]
    sidebar = spec.get("regions", {}).get("sidebar")
    if sidebar:
        for a in columns:
            if _geometry(a)["x_pt"] < sidebar["x_pt"]:
                ct.set_position(a, x=sidebar["x_pt"])
                ct.set_extent(a, width=sidebar["width_pt"])
            props = _body(a).find(emit.WPS + 'bodyPr')
            props.set('lIns', '0')
            props.set('rIns', '0')
    plain_refs = {}
    for ref in spec.get("header_plain", {}).values():
        key = (ref["anchor"], ref.get("shape"))
        if key not in plain_refs:
            plain_refs[key] = (ref, ct.box_anchor(root, ref))
    # 联系方式原件可能只有值；仅生成副本增加标签，随后进入同一对齐逻辑。
    unlabelled = spec.get("header_unlabelled") or {}
    for key, ref in unlabelled.items():
        for source, copied in zip(refs, columns):
            if source["anchor"] != ref["anchor"]:
                continue
            paragraphs = _body(copied).findall(".//" + emit.W + "txbxContent/" + emit.W + "p")
            if ref["paragraph"] < len(paragraphs):
                p = paragraphs[ref["paragraph"]]
                value = "".join(t.text or "" for t in p.iter(emit.W + "t"))
                if emit._split_label_value(value) is None:
                    _set_label_value(p, ref.get("label", key), value)
    fields = dict(header.get("fields") or {})
    hidden = set(header.get("hidden_fields") or [])
    custom = {item["key"]: item for item in header.get("custom_fields") or []}
    labels = dict(spec.get("header_labels") or {})
    for key, ref in unlabelled.items():
        labels[ref.get("label", key)] = key
    column_header = dict(header)
    # 无标签姓名/职位留在各自框中，不能再作为自定义行写入普通列。
    column_header["custom_fields"] = [v for k, v in custom.items() if k not in spec.get("header_plain", {})]
    present = set(spec.get("header_plain", {}))
    for a in columns:
        for p in _body(a).findall(".//" + emit.W + "txbxContent/" + emit.W + "p"):
            pair = emit._split_label_value("".join(t.text or "" for t in p.iter(emit.W + "t")))
            if pair:
                present.add(labels.get("".join(pair[0].split())))
    readable = {value: key for key, value in {**emit.INFO_FIELD_LABELS, **labels}.items()}
    readable.setdefault("target_role", "求职意向")
    for key, value in fields.items():
        if key not in present and key not in custom and key not in hidden and value:
            column_header["custom_fields"].append({"key": key, "label": readable.get(key, key), "value": str(value)})
    records = apply_header_components([_body(a) for a in columns], labels, column_header) if columns else {}
    # 列表编号自带的首个 tab 会把标签推到值列；字段使用自身标签/值 tab。
    for a in columns:
        for p in _body(a).findall(".//" + emit.W + "txbxContent/" + emit.W + "p"):
            props = p.find(emit.W + "pPr")
            if props is None:
                continue
            for old in list(props.findall(emit.W + "numPr")):
                props.remove(old)
            numbering = emit.etree.SubElement(props, emit.W + "numPr")
            emit.etree.SubElement(numbering, emit.W + "numId").set(emit.W + "val", "0")
    plain_paragraphs = {}
    for key, ref in spec.get("header_plain", {}).items():
        a = plain_refs[(ref["anchor"], ref.get("shape"))][1]
        paragraphs = _body(a).findall(".//" + emit.W + "txbxContent/" + emit.W + "p")
        if ref["paragraph"] < len(paragraphs):
            plain_paragraphs[key] = (ref, a, paragraphs[ref["paragraph"]])
    for key, (ref, a, paragraph) in plain_paragraphs.items():
        before = "".join(t.text or "" for t in paragraph.iter(emit.W + "t"))
        pair = emit._split_label_value(before)
        original = pair[1] if pair else before
        if key in hidden:
            paragraph.getparent().remove(paragraph)
            continue
        value = str(custom.get(key, {}).get("value", fields.get(key, original)))
        runs = list(paragraph.iter(emit.W + "r"))
        sample = copy.deepcopy(runs[-1]) if runs else emit.etree.Element(emit.W + "r")
        for run in runs:
            run.getparent().remove(run)
        emit._set_run_text(sample, value)
        paragraph.append(sample)
        apply_field_style(paragraph, custom.get(key, {}), value_only=False)
        records[key] = {"before": original, "after": value, "label": "",
                        "column": "plain_" + str(ref["anchor"]), "standalone": True}
    ct.strip_boxes(root, refs + [r for r, _ in plain_refs.values()])
    objects = {}
    fits = (header.get("fit") or {}).get("columns", {})
    for index, a in enumerate(columns):
        key = "column_" + str(index)
        objects[key] = a
    for (ident, shape), (_, a) in plain_refs.items():
        key = f"plain_{ident}_{shape}"
        objects[key] = a
        tx = _body(a).find(".//" + emit.W + "txbxContent")
        if not tx.findall(emit.W + "p"):
            emit.etree.SubElement(tx, emit.W + "p")
    for key, a in objects.items():
        a.find(emit.WP + "docPr").set("name", "ResumeHeader_" + key)
        props = _body(a).find(emit.WPS + "bodyPr")
        props.set("anchor", "t")
        props.set("anchorCtr", "0")
        for child in list(props):
            if emit.etree.QName(child).localname in {"spAutoFit", "normAutofit"}:
                props.remove(child)
        if key in fits:
            fit = fits[key]
            ct.set_position(a, x=fit["x_pt"], y=fit["y_pt"])
            ct.set_extent(a, width=fit["width_pt"], height=fit["height_pt"])
        ct.append_anchor(root, a)
        a.find(emit.WP + "docPr").set("name", "ResumeHeader_" + key)
    for key, value in (header.get("fit") or {}).get("titles", {}).items():
        ct.set_position(ct.anchor(root, int(key)), y=value)
    for ident in (header.get("fit") or {}).get("hidden_titles", []):
        title = ct.anchor(root, ident)
        drawing = title.getparent()
        drawing.remove(title)
        if not len(drawing):
            drawing.getparent().remove(drawing)
    return records, objects


def _set_photo(root, template: Path, spec: dict, header: dict, overrides: dict):
    from skill_toolbox.resume_layout import component_template as ct

    ref = spec.get("photo")
    if not ref:
        return None
    photo = ct.anchor(root, ref["anchor"])
    if header.get("hide_photo"):
        drawing = photo.getparent()
        drawing.remove(photo)
        if not len(drawing):
            drawing.getparent().remove(drawing)
        return None
    geometry = _geometry(photo)
    fit = (header.get("fit") or {}).get("photo") or {}
    if fit:
        ct.set_position(photo, x=fit.get("x_pt", geometry["x_pt"]), y=fit.get("y_pt", geometry["y_pt"]))
    data = header.get("photo_bytes")
    if not data and fit:
        with zipfile.ZipFile(template) as package:
            data = package.read(ref["part"])
    if not data:
        return None
    grouped = photo.find('.//' + emit.WPG + 'wgp') is not None
    if grouped:
        from skill_toolbox.resume_layout.typography import scale_anchor
        factor = min(fit.get('width_pt', geometry['width_pt']) / geometry['width_pt'],
                     fit.get('height_pt', geometry['height_pt']) / geometry['height_pt'])
        scale_anchor(photo, factor)
        info = emit.replace_photo(photo, data)
    else:
        info = emit.replace_photo(photo, data, frame_cx_pt=fit.get("width_pt", geometry["width_pt"]),
                                  frame_cy_pt=fit.get("height_pt", geometry["height_pt"]))
        ct.set_extent(photo, width=info["width_pt"], height=info["height_pt"])
        ct.set_position(photo, x=fit.get("x_pt", geometry["x_pt"]) + info["off_x_pt"],
                        y=fit.get("y_pt", geometry["y_pt"]) + info["off_y_pt"])
        pic = photo.find(".//{http://schemas.openxmlformats.org/drawingml/2006/picture}spPr/" + emit.A + "xfrm/" + emit.A + "off")
        if pic is not None:
            pic.set("x", "0")
            pic.set("y", "0")
    overrides[ref["part"]] = data
    with zipfile.ZipFile(template) as package:
        types = emit.etree.fromstring(package.read("[Content_Types].xml"))
    node = next((n for n in types if n.get("PartName") == "/" + ref["part"]), None)
    if node is None:
        node = emit.etree.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override")
    node.set("PartName", "/" + ref["part"])
    from io import BytesIO

    from PIL import Image
    with Image.open(BytesIO(data)) as source:
        content_type = Image.MIME.get(source.format, "image/png")
    node.set("ContentType", content_type)
    overrides["[Content_Types].xml"] = emit.etree.tostring(types, encoding="utf-8", xml_declaration=True)
    return info


def apply_header(root, template: Path, spec: dict, header: dict, overrides: dict) -> dict:
    fields, _ = _rewrite(root, spec, header)
    result = {"fields": fields, "fit": header.get("fit") or {}}
    photo = _set_photo(root, template, spec, header, overrides)
    if photo:
        result["photo"] = photo
    return result


def _measure_shape(shape) -> dict:
    """大框只用于暴露全部文字，容量由实际行位决定。"""
    frame, height = shape.TextFrame, 7.2
    lines = 0
    if frame.HasText:
        text = frame.TextRange
        for index in range(1, text.Paragraphs.Count + 1):
            rng = text.Paragraphs(index).Range
            if not rng.Text.strip("\r\n\x07 "):
                continue
            count = max(1, int(rng.ComputeStatistics(1)))
            pitch = float(rng.ParagraphFormat.LineSpacing)
            if pitch <= 0 or pitch > 100:
                pitch = 18.0
            top = float(rng.Characters(1).Information(6))
            if top < 0:
                raise ValueError("HEADER_OVERFLOW: 无法测得个人信息段落实际行位")
            # auto 行距的 LineSpacing 值不是最终字形行高；末字符真实行位捕获换行。
            last_index = max(1, len(rng.Text.rstrip("\r\n\x07 ")))
            last = rng.Characters(last_index)
            last_top = float(last.Information(6))
            font_size = float(last.Font.Size)
            if font_size <= 0 or font_size > 100:
                font_size = pitch
            if last_top < 0:
                raise ValueError("HEADER_OVERFLOW: 无法测得个人信息末行实际行位")
            text_bottom = max(top + count * pitch, last_top + max(font_size, pitch))
            height = max(height, text_bottom - float(shape.Top) + float(frame.MarginBottom))
            lines += count
    if frame.Overflowing:
        raise ValueError("HEADER_OVERFLOW: 个人信息文字超出当前页可用高度")
    # Word 的自动行距还包含不可见段尾行框；在真实行位下界上验证最小容纳框。
    capacity = float(shape.Height)
    shape.Height = min(capacity, height)
    if shape.TextFrame.Overflowing:
        low, high = float(shape.Height), capacity
        while high - low > 0.5:
            middle = (low + high) / 2
            shape.Height = middle
            if shape.TextFrame.Overflowing:
                low = middle
            else:
                high = middle
        height = high
    # 保留半点避免 OOXML twip 舍入恰落在 Word 裁切阈值。
    height = min(capacity, height + 0.5)
    return {"height_pt": round(height, 2), "lines": lines}


def fit_header(template: Path, spec: dict, header: dict, work_dir: Path) -> dict:
    from skill_toolbox.resume_layout import component_template as ct
    from skill_toolbox.word_com import close_word, start_word

    root = ct.load_normalized(template, spec)
    clean = dict(header)
    clean.pop("fit", None)
    _, objects = _rewrite(root, spec, clean)
    bottom = min(r["bottom_pt"] for r in spec["regions"].values())
    originals = {key: _geometry(a) for key, a in objects.items()}
    # 页内扩高供 Word 暴露所有文字，不把原框高度误当内容高度。
    for key, a in objects.items():
        ct.set_extent(a, height=max(8.0, bottom - originals[key]["y_pt"]))
        props = _body(a).find(emit.WPS + "bodyPr")
        for child in list(props):
            if emit.etree.QName(child).localname in {"spAutoFit", "normAutofit"}:
                props.remove(child)
    overrides = {}
    _set_photo(root, template, spec, clean, overrides)
    work_dir.mkdir(parents=True, exist_ok=True)
    host = work_dir / "header_host.docx"
    emit.save_document_xml(root, template, host, part_overrides=overrides or None)
    word, doc = start_word(), None
    columns = {}
    try:
        doc = word.Documents.Open(str(host.resolve()), False, True)
        doc.Repaginate()
        for key, geometry in originals.items():
            measured = _measure_shape(doc.Shapes("ResumeHeader_" + key))
            columns[key] = {**geometry, **measured}
    finally:
        close_word(word, doc)
    result = {"columns": columns, "titles": {}, "hidden_titles": [], "body_top_by_region": {}}
    photo = spec.get("photo")
    photo_geometry = _geometry(ct.anchor(root, photo["anchor"])) if photo and not header.get("hide_photo") else None
    groups = spec.get("header_groups") or []
    if groups:
        # 双栏头部按组独立向下排，右主栏的首位置完全不受影响。
        refs = spec.get("header_columns", [])
        first_title = ct.anchor(root, groups[0]["title_ids"][0])
        cursor = _geometry(first_title)["y_pt"]
        if header.get("hide_photo") and spec.get("header_plain"):
            plain = [v for k, v in columns.items() if k.startswith("plain_")]
            if plain:
                first = min(p["y_pt"] for p in plain)
                top = spec["positions"][spec["photo"]["anchor"]][1]
                for geometry in plain:
                    geometry["y_pt"] += top - first
                cursor = max(p["y_pt"] + p["height_pt"] for p in plain) + 16
        else:
            plain_bottom = max((v["y_pt"] + v["height_pt"] for k, v in columns.items() if k.startswith("plain_")), default=0)
            cursor = max(cursor, plain_bottom + 12)
        for group in groups:
            title_y = min(_geometry(ct.anchor(root, i))["y_pt"] for i in group["title_ids"])
            members = [("column_" + str(i), r) for i, r in enumerate(refs) if r["anchor"] in group["columns"]]
            if members and not any(columns[key]["lines"] for key, _ in members):
                result["hidden_titles"].extend(group["title_ids"])
                continue
            for ident in group["title_ids"]:
                result["titles"][str(ident)] = round(cursor + _geometry(ct.anchor(root, ident))["y_pt"] - title_y, 2)
            offsets = [originals[key]["y_pt"] - title_y for key, _ in members]
            for (key, _), offset in zip(members, offsets):
                columns[key]["y_pt"] = round(cursor + offset, 2)
            cursor = max((columns[key]["y_pt"] + columns[key]["height_pt"] for key, _ in members), default=cursor + 22.35) + 16
        result["body_top_by_region"] = {"main": spec["regions"]["main"]["top_pt"], "sidebar": round(cursor, 2)}
    else:
        text_bottom = max((v["y_pt"] + v["height_pt"] for v in columns.values()), default=0)
        if photo_geometry:
            photo_geometry["height_pt"] = min(photo_geometry["height_pt"], max(72.0, text_bottom - photo_geometry["y_pt"]))
            photo_geometry['y_pt'] = max(photo_geometry['y_pt'], text_bottom - photo_geometry['height_pt'])
            result["photo"] = photo_geometry
        header_bottom = max(text_bottom, (photo_geometry["y_pt"] + photo_geometry["height_pt"]) if photo_geometry else 0)
        start = round(header_bottom + 12, 2)
        result["body_top_pt"] = start
        result["body_top_by_region"] = {"main": start}
    if max(result["body_top_by_region"].values(), default=0) + 32 > bottom:
        raise ValueError("HEADER_OVERFLOW: 个人信息占用后，本页没有完整栏目标题和首行的空间")
    return result
