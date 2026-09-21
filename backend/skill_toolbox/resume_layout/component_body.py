"""组件模板的正文测量和输出；两阶段共用同一文本/样式构造路径。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from skill_toolbox.resume_layout import component_header, emit, layout, typography
from skill_toolbox.resume_layout import component_template as ct
from skill_toolbox.word_com import close_document, close_word, start_word


def _body(anchor):
    return next(anchor.iter(emit.WPS + "wsp"))


def _part_entry(entry, text, heading_lines, index_shift=0):
    result = {**entry, "text": text, "has_heading": bool(heading_lines), "heading_lines": heading_lines}
    for key in ("detail_styles", "paragraph_styles"):
        result[key] = {int(k) + index_shift: v for k, v in (entry.get(key) or {}).items()
                       if int(k) + index_shift >= 0}
    tech = entry.get("tech_stack_line")
    result["tech_stack_line"] = tech + index_shift if tech is not None else None
    return result


def _head_spans(entry, fields, template_id):
    """把原三槽标题的范围投影到新的字段顺序，包含同值字段与多行值。"""
    from skill_toolbox.resume_layout.profiles import get_profile
    count = int(entry.get("heading_lines") or 0)
    source_lines = entry["text"].splitlines()[:count]
    source_heading = "\n".join(source_lines)
    offsets, cursor = [], 0
    for line in source_lines:
        offsets.append(cursor); cursor += len(line) + 1
    origins, cursor = {}, 0
    for key in get_profile(template_id).header_slot_order:
        value = str((entry.get("head") or {}).get(key) or "").strip()
        if not value:
            continue
        start = source_heading.find(value, cursor)
        if start >= 0:
            origins[key] = start
            cursor = start + len(value)
    result, target_paragraph = {}, 0
    for key in fields:
        value = str((entry.get("head") or {}).get(key) or "").strip()
        if not value:
            continue
        start = origins.get(key)
        value_lines = value.splitlines()
        if start is not None:
            for source_index, spans in (entry.get("paragraph_styles") or {}).items():
                source_index = int(source_index)
                if source_index >= len(offsets):
                    continue
                for span in spans:
                    left = max(start, offsets[source_index] + int(span["start"]))
                    right = min(start + len(value), offsets[source_index] + int(span["end"]))
                    if left >= right:
                        continue
                    line_offset = 0
                    for line_index, line in enumerate(value_lines):
                        lo = max(left - start, line_offset)
                        hi = min(right - start, line_offset + len(line))
                        if lo < hi:
                            result.setdefault(target_paragraph + line_index, []).append(
                                {**span, "start": lo - line_offset, "end": hi - line_offset})
                        line_offset += len(line) + 1
        target_paragraph += len(value_lines)
    return result


def _parts(root, section, entry, spec, template_id, numbering):
    """返回完整格式的独立框；side/main 同时缩放、测量、写入。"""
    config = spec["sections"][ct.source_key(section, spec)]
    region = spec["regions"][section.get("region", config.get("region", "main"))]
    section = {**section, "body_offset_pt": section.get("body_offset_pt", config["body_offset_pt"]),
               "line_pitch_pt": section.get("line_pitch_pt", config.get("line_pitch_pt", spec["line_pitch_pt"]))}
    main = ct.box_anchor(root, config["body"])
    split = bool(config.get("side_body") and entry.get("head") and section["id"] != "projects")
    raw_parts = []
    if split:
        head = entry["head"]
        old_head_count = int(entry.get("heading_lines") or 0)
        details = entry["text"].splitlines()[old_head_count:]
        org = str(head.get("org") or "").strip()
        org_lines = org.splitlines() if org else []
        main_lines = org_lines + details
        main_entry = _part_entry(entry, "\n".join(main_lines), len(org_lines), len(org_lines) - old_head_count)
        main_entry["paragraph_styles"] = {int(k) + len(org_lines) - old_head_count: v
                                          for k, v in (entry.get("paragraph_styles") or {}).items()
                                          if int(k) >= old_head_count}
        main_entry["paragraph_styles"].update(_head_spans(entry, ("org",), template_id))
        side_lines = [str(head.get(k) or "").strip() for k in ("role", "date") if str(head.get(k) or "").strip()]
        side_entry = _part_entry(entry, "\n".join(side_lines), 1 if side_lines else 0)
        side_entry["detail_styles"] = {}
        side_entry["paragraph_styles"] = _head_spans(entry, ("role", "date"), template_id)
        side_entry["inline_styles"] = []
        side_entry["tech_stack_line"] = None
        if side_lines:
            raw_parts.append(("side", ct.box_anchor(root, config["side_body"]), side_entry))
        if main_lines:
            raw_parts.append(("main", main, main_entry))
        if not raw_parts:
            raw_parts.append(("main", main, dict(entry)))
    else:
        if config.get("side_body"):
            # 项目标题/技术栈需要整个主流的宽度。
            ct.set_position(main, x=region["x_pt"])
            ct.set_extent(main, width=region["width_pt"])
        raw_parts.append(("main", main, dict(entry)))
    # 对双区条目，width_pt表示整个组件宽度，两侧及中间间隔按同一比例调整。
    base_left = min(ct.position(a)[0] for _, a, _ in raw_parts)
    base_right = max(ct.position(a)[0] + ct.extent(a)[0] for _, a, _ in raw_parts)
    base_width = base_right - base_left
    section_scale = float(section.get("scale") or 1)
    entry_scale = float(entry.get("scale") or 1)
    width_ratio = float(entry.get("width_pt") or (base_width * section_scale * entry_scale)) / base_width
    # 源模板有最多约1.5pt的框缘偏差，保留其本来宽度；新增宽度不得侵入另一流。
    available_right = max(base_right, region["x_pt"] + region["width_pt"])
    if base_left + base_width * width_ratio > available_right + 0.05:
        raise ValueError(f"TYPOGRAPHY_BOUNDS: 条目 {entry['id']} 的宽度超出 {section.get('region', config.get('region', 'main'))} 排版区域")
    parts = []
    for name, anchor, data in raw_parts:
        x, _ = ct.position(anchor)
        width, _ = ct.extent(anchor)
        body = _body(anchor)
        typography.scale_title(anchor, section_scale, body=body, template_id=template_id)
        if split:
            data["width_pt"] = width * width_ratio
            ct.set_position(anchor, x=base_left + (x - base_left) * width_ratio)
        style_snaps = None
        if name == 'side':
            # 分列模板的方块属于岗位标题；日期用其自身的普通段落样式。
            # 通用 numPr 分桶会恰好把这两个角色反过来。
            original = emit._para_style_snapshots(body.find('.//' + emit.W + 'txbxContent'))
            style_snaps = [original[min(index, len(original) - 1)]
                           for index, field in enumerate(('role', 'date'))
                           for line in str(entry['head'].get(field) or '').strip().splitlines() if line]
        emit.set_box_text(body, data["text"], first_is_header=data.get("has_heading"),
                          style_snaps=style_snaps,
                          header_lines=data.get("heading_lines"), tech_stack_line=data.get("tech_stack_line"),
                          detail_styles=data.get("detail_styles"), inline_styles=data.get("inline_styles"),
                          paragraph_styles=data.get("paragraph_styles"))
        metrics = typography.apply_body(body, data, section, template_id, numbering)
        # 固定行距使单段 wrap 与多段计算使用相同的可验证几何。
        for index, paragraph in enumerate(body.findall(".//" + emit.W + "txbxContent/" + emit.W + "p")):
            ppr = paragraph.find(emit.W + "pPr")
            if ppr is None:
                ppr = emit.etree.Element(emit.W + "pPr"); paragraph.insert(0, ppr)
            if name != 'side' and index < int(data.get('heading_lines') or 0):
                indent = ppr.find(emit.W + 'ind')
                if indent is not None:
                    for key in ('firstLine', 'firstLineChars', 'hanging', 'hangingChars'):
                        indent.attrib.pop(emit.W + key, None)
            spacing = ppr.find(emit.W + "spacing")
            if spacing is None:
                spacing = emit.etree.SubElement(ppr, emit.W + "spacing")
            spacing.set(emit.W + "line", str(round(metrics["line_pitch_pt"] * 20)))
            spacing.set(emit.W + "lineRule", "exact")
            spacing.set(emit.W + "before", "0"); spacing.set(emit.W + "after", "0")
        props = body.find(emit.WPS + "bodyPr")
        for child in list(props):
            if emit.etree.QName(child).localname in {"spAutoFit", "normAutofit"}:
                props.remove(child)
        props.set("anchor", "t")
        ct.set_extent(anchor, width=metrics["body_width_pt"])
        parts.append({"name": name, "anchor": anchor, "entry": data, "metrics": metrics,
                      "measurement_id": f"{entry['id']}::{name}", "component_width_pt": base_width * width_ratio})
    return parts


def _empty_host(source_root):
    root = copy.deepcopy(source_root)
    ct.remove_anchors(root, [int(a.find(emit.WP + "docPr").get("id")) for a in root.iter(emit.WP + "anchor")])
    return root


def measure_scenario(scenario: dict, work_dir: Path, *, template: Path, template_id: str) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    spec = ct.get_spec(template_id)
    source_root = ct.load_normalized(template, spec)
    hosts = []
    for section in scenario["sections"]:
        for entry in section["entries"]:
            numbering = typography.Numbering(template)
            for part in _parts(source_root, section, entry, spec, template_id, numbering):
                root = _empty_host(source_root)
                anchor = part["anchor"]
                top = 55.0
                ct.set_position(anchor, y=top)
                ct.set_extent(anchor, height=spec["page_height_pt"] - top - 35.0)
                anchor = ct.append_anchor(root, anchor)
                anchor.find(emit.WP + "docPr").set("name", "ResumeMeasureBody")
                host = work_dir / f"measure-component-{len(hosts)}.docx"
                emit.save_document_xml(root, template, host, part_overrides=numbering.parts() or None)
                hosts.append({"entry_id": entry["id"], "section_id": section["id"], "part": part["name"],
                              "measurement_id": part["measurement_id"], "text": part["entry"]["text"],
                              "host": str(host.resolve()), "body_top_pt": top,
                              "component_width_pt": part["component_width_pt"],
                              "box": [*ct.position(anchor), *ct.extent(anchor)], **part["metrics"]})
    rows = []
    word = start_word()
    try:
        for host in hosts:
            doc = word.Documents.Open(host["host"], False, True)
            shape = paras = rng = None
            try:
                doc.Repaginate()
                shape = doc.Shapes("ResumeMeasureBody")
                if shape.TextFrame.Overflowing:
                    raise ValueError(f"LAYOUT_OVERFLOW: 条目 {host['entry_id']} 的 {host['part']} 单框超过一页")
                paras = shape.TextFrame.TextRange.Paragraphs
                tops, counts = [], []
                for i in range(1, paras.Count + 1):
                    rng = paras(i).Range
                    tops.append(float(rng.Characters(1).Information(6)))
                    counts.append(max(1, int(rng.ComputeStatistics(1))))
                if min(tops, default=-1) < 0:
                    raise ValueError(f"MEASURE_INVALID: 条目 {host['entry_id']} 无法测得真实行位")
                pitch = host["line_pitch_pt"]
                bottom = max(top + count * pitch for top, count in zip(tops, counts))
                rows.append({**host, "wrapped_lines": sum(counts), "paragraphs": len(counts),
                             "first_line_top_pt": tops[0], "last_line_top_pt": tops[-1],
                             "text_top_offset_pt": round(tops[0] - host["body_top_pt"], 3),
                             "text_height_pt": round(bottom - tops[0], 3),
                             "text_bottom_offset_pt": round(bottom - host["body_top_pt"], 3)})
            finally:
                rng = paras = shape = None
                close_document(doc)
                doc = None
    finally:
        close_word(word)
    results = []
    for section in scenario["sections"]:
        for entry in section["entries"]:
            parts = [r for r in rows if r["entry_id"] == entry["id"]]
            top = min(r["text_top_offset_pt"] for r in parts)
            bottom = max(r["text_bottom_offset_pt"] for r in parts)
            results.append({"entry_id": entry["id"], "wrapped_lines": max(r["wrapped_lines"] for r in parts),
                            "line_pitch_pt": min(r["line_pitch_pt"] for r in parts),
                            "text_height_pt": bottom - top, "text_top_offset_pt": top,
                            "body_width_pt": parts[0]["component_width_pt"],
                            "body_offset_pt": parts[0]["body_offset_pt"],
                            "body_pad_pt": max(r["body_pad_pt"] for r in parts)})
    (work_dir / "component_measurements.json").write_text(json.dumps({"template_id": template_id, "parts": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "measure_result.json").write_text(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    fit = component_header.fit_header(template, spec, scenario.get("header") or {}, work_dir)
    (work_dir / "header_fit.json").write_text(json.dumps(fit, ensure_ascii=False), encoding="utf-8")
    return {r["entry_id"]: layout.MeasureResult.from_dict(r) for r in results}


def _title_sources(root, config):
    titles = []
    for ident in config["title_ids"]:
        source = ct.anchor(root, ident)
        title = copy.deepcopy(source)
        refs = [config[key] for key in ("body", "side_body") if key in config and config[key]["anchor"] == ident]
        shapes = list(title.iter(emit.WPS + "wsp"))
        for ref in sorted(refs, key=lambda r: r.get("shape") or 0, reverse=True):
            shape = shapes[ref["shape"]]
            shape.getparent().remove(shape)
        titles.append(title)
    return titles


def emit_scenario(scenario: dict, plan: layout.LayoutPlan, out_docx: Path, *, template: Path, template_id: str) -> dict:
    spec = ct.get_spec(template_id)
    source = ct.load_normalized(template, spec)
    root = copy.deepcopy(source)
    numbering = typography.Numbering(template)
    remove = set(spec.get("extra_remove_ids", []))
    for config in spec["sections"].values():
        remove.update(config["title_ids"])
        remove.add(config["body"]["anchor"])
        if config.get("side_body"):
            remove.add(config["side_body"]["anchor"])
    ct.remove_anchors(root, remove)
    body = root.find(emit.W + "body")
    carriers = {0: body.find(emit.W + "p")}
    previous = carriers[0]
    for page in range(1, plan.pages):
        brk = emit.etree.Element(emit.W + "p")
        emit.etree.SubElement(emit.etree.SubElement(brk, emit.W + "r"), emit.W + "br", {emit.W + "type": "page"})
        previous.addnext(brk)
        carrier = emit.etree.Element(emit.W + "p")
        props = emit.etree.SubElement(carrier, emit.W + "pPr")
        emit.etree.SubElement(props, emit.W + "spacing", {emit.W + "line": "20", emit.W + "lineRule": "exact"})
        brk.addnext(carrier); carriers[page] = carrier; previous = carrier
        for ident in spec["decorations"]:
            original = ct.anchor(source, ident)
            cloned = ct.append_anchor(root, copy.deepcopy(original), carrier)
            cloned.set("relativeHeight", original.get("relativeHeight"))
    records, emitted_parts, component_ids = [], [], set()
    for section_plan in plan.sections:
        section = next(s for s in scenario["sections"] if s["id"] == section_plan.section_id)
        config = spec["sections"][ct.source_key(section, spec)]
        titles = _title_sources(source, config)
        origin_x, origin_y = ct.position(titles[0])
        factor = float(section.get("scale") or 1)
        for i, title in enumerate(titles):
            x, y = ct.position(title)
            if i == 0:
                box = next((b for b in title.iter(emit.WPS + "wsp") if b.find(".//" + emit.W + "t") is not None), None)
                if box is not None:
                    emit.set_box_text(box, section["title"])
            typography.scale_title(title, factor, template_id=template_id)
            ct.set_position(title, x=origin_x + (x-origin_x)*factor, y=section_plan.anchor_y_pt + (y-origin_y)*factor)
            ct.append_anchor(root, title, carriers[section_plan.page_index])
        for entry_plan in section_plan.entries:
            entry = next(e for e in section["entries"] if e["id"] == entry_plan.instance_id)
            for part in _parts(source, section, entry, spec, template_id, numbering):
                anchor = part["anchor"]
                ct.set_position(anchor, y=entry_plan.anchor_y_pt)
                ct.set_extent(anchor, height=entry_plan.body_h_pt)
                ct.append_anchor(root, anchor, carriers[entry_plan.page_index])
                component_ids.add(anchor.find(emit.WP + "docPr").get("id"))
                emitted_parts.append({"instance_id": entry["id"], "section_id": section["id"],
                                      "part": part["name"], "measurement_id": part["measurement_id"],
                                      "text": part["entry"]["text"], "page": entry_plan.page_index,
                                      "box": [*ct.position(anchor), *ct.extent(anchor)],
                                      "line_pitch_pt": part["metrics"]["line_pitch_pt"]})
            records.append({"instance_id": entry["id"], "page": entry_plan.page_index})
    overrides = numbering.parts()
    header = component_header.apply_header(root, template, spec, scenario.get("header") or {}, overrides)
    fixed_texts = []
    for anchor in root.iter(emit.WP + "anchor"):
        if anchor.find(emit.WP + "docPr").get("id") in component_ids:
            continue
        if next(anchor.iter(emit.WPS + "wsp"), None) is None:
            continue
        for shape, (_, _, width, height) in ct._shape_bounds(anchor):
            if width <= 0.01 or height <= 0.01:
                continue
            for paragraph in shape.iter(emit.W + "p"):
                text = "".join(t.text or "" for t in paragraph.iter(emit.W + "t"))
                if text.strip():
                    fixed_texts.append(text)
    components = [{"id": part["measurement_id"], "entry_id": part["instance_id"],
                   "text": part["text"], "page_index": part["page"],
                   "x_pt": part["box"][0], "y_pt": part["box"][1],
                   "width_pt": part["box"][2], "height_pt": part["box"][3],
                   "measurement_id": part["measurement_id"]} for part in emitted_parts]
    emit.save_document_xml(root, template, out_docx, part_overrides=overrides or None)
    return {"pages": plan.pages, "header": header, "emit_records": records, "boxes": [],
            "parts": emitted_parts, "components": components, "fixed_texts": fixed_texts}
