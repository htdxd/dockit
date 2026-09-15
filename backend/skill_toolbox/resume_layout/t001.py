"""t001：独立标题组与正文框的具体适配，复用共享文字/布局/渲染流程。"""
from __future__ import annotations

import copy
import zipfile
from pathlib import Path

from skill_toolbox.resume_layout import emit, layout, spacing, typography
from skill_toolbox.resume_layout.profiles import get_profile
from skill_toolbox.resume_layout.header import apply_header_components
from skill_toolbox.tools.workspace import sha256_file

# docPr.id 唯一；原件多个独立正文框具有同名 Rectangle 3。
SECTIONS = {
    "summary": (40, 21, 25.2), "education": (11, 18, 26.25),
    "work": (12, 20, 29.4), "skills": (35, 19, 26.2),
}
DECORATIONS = (14, 7, 15, 3, 33)


def fixed_texts(template: Path, *, pages: int = 1, include_fields: bool = True) -> list[str]:
    root = emit.load_document_xml(template)
    variable = {i for values in SECTIONS.values() for i in values[:2]} | {8}
    texts = []
    for a in root.iter(emit.WP + "anchor"):
        ident = int(a.find(emit.WP + "docPr").get("id"))
        if ident in variable or (not include_fields and ident in (22, 62)):
            continue
        for p in a.iter(emit.W + "p"):
            text = "".join(t.text or "" for t in p.iter(emit.W + "t"))
            if not text.strip():
                continue
            # 标签和值分开：替换值之后标签仍在原位置。
            texts.extend(text.split("：", 1) * (pages if ident in DECORATIONS else 1))
    return texts


def source_key(section: dict) -> str:
    key = section["id"]
    if key in SECTIONS:
        return key
    return "skills" if section.get("prototype") == "plain_lines_v1" else "work"


def anchor_by_id(root, ident: int):
    return next(a for a in root.iter(emit.WP + "anchor")
                if a.find(emit.WP + "docPr").get("id") == str(ident))


def alternate(anchor):
    node = anchor
    while node is not None and emit.etree.QName(node).localname != "AlternateContent":
        node = node.getparent()
    if node is None:
        raise ValueError("t001 anchor 缺少 AlternateContent")
    return node


def body_for(root, section: dict):
    return emit.find_body_wsp(anchor_by_id(root, SECTIONS[source_key(section)][1]))


def set_page_position(anchor, *, x: float | None = None, y: float | None = None):
    for axis, value in (("H", x), ("V", y)):
        if value is None:
            continue
        position = anchor.find(emit.WP + "position" + axis)
        position.set("relativeFrom", "page")
        position.find(emit.WP + "posOffset").text = str(emit.pt2emu(value))


def normalize_positions(root):
    # 原件全部挂在首段落：Word 校准首段落 y=72，column x=90pt。
    for anchor in root.iter(emit.WP + "anchor"):
        h = anchor.find(emit.WP + "positionH")
        v = anchor.find(emit.WP + "positionV")
        x = int(h.find(emit.WP + "posOffset").text) / emit.EMU
        y = int(v.find(emit.WP + "posOffset").text) / emit.EMU
        set_page_position(anchor, x=x + (90 if h.get("relativeFrom") == "column" else 0),
                          y=y + (72 if v.get("relativeFrom") == "paragraph" else 0))


def configure_heading_tabs(wsp, entry: dict):
    typography.configure_heading_tabs(wsp, entry, template_id="t001")


def fit_header(template: Path, fields: dict, work_dir: Path, *, header: dict | None = None) -> dict:
    """保持字体，使用两列之间及正文之前的现有空隙；Word 仍溢出则明确失败。"""
    import pythoncom
    from win32com import client

    root = emit.load_document_xml(template)
    normalize_positions(root)
    profile = get_profile("t001")
    boxes = []
    for ident in (22, 62):
        anchor = anchor_by_id(root, ident)
        anchor.find(emit.WP + "docPr").set("name", f"ResumeHeader{ident}")
        boxes.append(emit.find_body_wsp(anchor))
    components = apply_header_components(boxes, profile.header_labels, header or {"fields": fields})
    host = work_dir / "header_host.docx"
    emit.save_document_xml(root, template, host)
    pythoncom.CoInitialize()
    word = client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    doc = None
    try:
        doc = word.Documents.Open(str(host.resolve()), False, True)
        left, right = doc.Shapes("ResumeHeader22"), doc.Shapes("ResumeHeader62")
        # 整框未溢出也可能把求职意向最后一个字挤到下一行。
        # 仅对实际已换行的意向使用列间现有空隙，原本一行的字段保持不变。
        target = components.get("target_role")
        if target and target["column"] == 0:
            paragraphs = left.TextFrame.TextRange.Paragraphs
            for i in range(1, paragraphs.Count + 1):
                rng = paragraphs(i).Range
                pair = emit._split_label_value(rng.Text)
                if (pair and "".join(pair[0].split()) == "".join(target["label"].split())
                        and rng.ComputeStatistics(1) > 1):
                    left.Width = max(left.Width, right.Left - left.Left - 7.2)
                    break
        results = {}
        for ident, shape in ((22, left), (62, right)):
            if shape.TextFrame.Overflowing and ident == 22:
                shape.Width = max(shape.Width, right.Left - shape.Left - 7.2)
            if shape.TextFrame.Overflowing:
                shape.Height = max(shape.Height, profile.geometry.page_top_pt - 2.0 - shape.Top)
            if shape.TextFrame.Overflowing:
                raise ValueError(
                    f"HEADER_OVERFLOW: t001 个人信息第 {1 if ident == 22 else 2} 列在保持原字号和栏目边界时仍放不下"
                )
            results[str(ident)] = {"width_pt": round(shape.Width, 2),
                                   "height_pt": round(shape.Height, 2), "overflow": False}
        if components and (header or {}).get("photo_bytes"):
            import pymupdf
            from skill_toolbox.resume_layout.header import rendered_field_bounds

            pdf_path = work_dir / "header_host.pdf"
            doc.ExportAsFixedFormat(str(pdf_path.resolve()), 17)
            with pymupdf.open(pdf_path) as pdf:
                text_bottom = max(b[3] for b in rendered_field_bounds(pdf[0], components))
            photo = anchor_by_id(root, 5)
            top = int(photo.find(emit.WP + "positionV/" + emit.WP + "posOffset").text) / emit.EMU
            original_height = emit.emu2pt(photo.find(emit.WP + "extent").get("cy"))
            # 信息少时不让原件大照片撑高首屏；至少保留一英寸高的容纳框。
            results["photo_height_pt"] = round(min(original_height, max(72.0, text_bottom - top)), 2)
        return results
    finally:
        if doc is not None:
            doc.Close(False)
        word.Quit()
        pythoncom.CoUninitialize()


def emit_scenario(scenario: dict, plan: layout.LayoutPlan, out_docx: Path, *, template: Path):
    profile = get_profile("t001")
    root = emit.load_document_xml(template)
    normalize_positions(root)
    numbering = typography.Numbering(template)
    sources = {ident: copy.deepcopy(alternate(anchor_by_id(root, ident)))
               for ident in {i for values in SECTIONS.values() for i in values[:2]}}
    # 旧第二条工作经历也必须移除。
    for ident in (*sources, 8):
        node = alternate(anchor_by_id(root, ident))
        node.getparent().remove(node)
    body = root.find(emit.W + "body")
    carrier = body.find(emit.W + "p")
    carriers = {0: carrier}
    for page in range(1, plan.pages):
        brp = emit.etree.Element(emit.W + "p")
        emit.etree.SubElement(emit.etree.SubElement(brp, emit.W + "r"),
                             emit.W + "br").set(emit.W + "type", "page")
        carrier.addnext(brp)
        carrier = emit.etree.Element(emit.W + "p")
        brp.addnext(carrier)
        carriers[page] = carrier

    def attach(ac, page):
        node = copy.deepcopy(ac)
        a = node.find(".//" + emit.WP + "anchor")
        next_id = max(emit._scan_max(root, "docPr", "id"),
                      emit._scan_max(root, "cNvPr", "id")) + 1
        emit.uniquify_ids(root, a, next_id)
        emit.etree.SubElement(carriers[page], emit.W + "r").append(node)
        return a

    for page in range(1, plan.pages):
        for ident in DECORATIONS:
            source = anchor_by_id(root, ident)
            decoration = attach(alternate(source), page)
            # ID 要唯一，但装饰的原始层次必须保留，否则后克隆的蓝底会盖住横幅文字。
            decoration.set("relativeHeight", source.get("relativeHeight"))
    records = []
    for sec in plan.sections:
        content = next(s for s in scenario["sections"] if s["id"] == sec.section_id)
        title_id, body_id, _ = SECTIONS[source_key(content)]
        title = attach(sources[title_id], sec.page_index)
        title_box = next(w for w in title.iter(emit.WPS + "wsp")
                         if w.find(".//" + emit.W + "txbxContent") is not None
                         and "".join(w.itertext()).strip())
        emit.set_box_text(title_box, content["title"])
        typography.scale_title(title, float(content.get("scale") or 1), template_id="t001")
        set_page_position(title, y=sec.anchor_y_pt)
        for entry in sec.entries:
            anchor = attach(sources[body_id], entry.page_index)
            wsp = emit.find_body_wsp(anchor)
            data = next(e for e in content["entries"] if e["id"] == entry.instance_id)
            typography.scale_title(anchor, float(content.get("scale") or 1),
                                    body=wsp, template_id="t001")
            emit.set_box_text(wsp, data["text"], first_is_header=data.get("has_heading"),
                              header_lines=data.get("heading_lines"), tech_stack_line=data.get("tech_stack_line"))
            metrics = typography.apply_body(wsp, data, content, "t001", numbering)
            anchor.find(emit.WP + "extent").set("cx", str(emit.pt2emu(metrics["body_width_pt"])))
            wsp.find(emit.WPS + "spPr/" + emit.A + "xfrm/" + emit.A + "ext").set(
                "cy", str(emit.pt2emu(entry.body_h_pt)))
            anchor.find(emit.WP + "extent").set("cy", str(emit.pt2emu(entry.body_h_pt)))
            set_page_position(anchor, y=entry.anchor_y_pt)
            records.append({"instance_id": entry.instance_id, "page": entry.page_index})
    header = scenario.get("header") or {}
    header_record = {"fields": apply_header_components(
        [emit.find_body_wsp(anchor_by_id(root, ident)) for ident in (22, 62)],
        profile.header_labels, header)}
    for ident in (22, 62):
        anchor = anchor_by_id(root, ident)
        fit = (header.get("fit") or {}).get(str(ident))
        if fit:
            box = emit.find_body_wsp(anchor)
            ext = box.find(emit.WPS + "spPr/" + emit.A + "xfrm/" + emit.A + "ext")
            for key, dimension in (("cx", "width_pt"), ("cy", "height_pt")):
                ext.set(key, str(emit.pt2emu(fit[dimension])))
                anchor.find(emit.WP + "extent").set(key, str(emit.pt2emu(fit[dimension])))
    header_record["fit"] = header.get("fit") or {}
    overrides = numbering.parts()
    photo = anchor_by_id(root, 5)
    if header.get("hide_photo"):
        drawing = photo.getparent()
        drawing.getparent().remove(drawing)
    elif header.get("photo_bytes"):
        info = emit.replace_photo(photo, header["photo_bytes"],
                                  frame_cy_pt=(header.get("fit") or {}).get("photo_height_pt"))
        extent = photo.find(emit.WP + "extent")
        extent.set("cx", str(emit.pt2emu(info["width_pt"])))
        extent.set("cy", str(emit.pt2emu(info["height_pt"])))
        h = photo.find(emit.WP + "positionH/" + emit.WP + "posOffset")
        v = photo.find(emit.WP + "positionV/" + emit.WP + "posOffset")
        set_page_position(photo, x=int(h.text) / emit.EMU + info["off_x_pt"],
                          y=int(v.text) / emit.EMU + info["off_y_pt"])
        # 独立图片的偏移由 anchor 控制，内部图像回到自身原点。
        picture_off = photo.find(".//" + "{http://schemas.openxmlformats.org/drawingml/2006/picture}spPr/"
                                 + emit.A + "xfrm/" + emit.A + "off")
        picture_off.set("x", "0")
        picture_off.set("y", "0")
        overrides[profile.photo_part] = header["photo_bytes"]
        with zipfile.ZipFile(template) as package:
            types = emit.etree.fromstring(package.read("[Content_Types].xml"))
        ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        override = next((e for e in types if e.get("PartName") == "/" + profile.photo_part), None)
        if override is None:
            override = emit.etree.SubElement(types, ns + "Override")
        override.set("PartName", "/" + profile.photo_part)
        override.set("ContentType", "image/png")
        overrides["[Content_Types].xml"] = emit.etree.tostring(types, encoding="utf-8", xml_declaration=True)
        header_record["photo"] = info
    emit.save_document_xml(root, template, out_docx, part_overrides=overrides or None)
    return {"pages": plan.pages, "header": header_record, "emit_records": records, "boxes": []}


def ensure_archive(template: Path, path: Path):
    """经 Word 参考原件核准的间距；哈希绑定，实际条目高度仍实时测量。"""
    if sha256_file(template) != "11f49b40d9c41edf9be19908ae592d105b442f60c4fbb060d192d21dcf980c02":
        raise ValueError("t001 原件哈希变化，需重新校准间距档案")
    if path.is_file():
        archive = spacing.load_archive(path)
        if archive.template_sha256 == sha256_file(template) and archive.template_id == "t001":
            return archive
    # COM 原件：自评272.8/3行，教育369.2/4行，工作末条616.4/5行。
    # 工作两条实际文字间距=616.4-(485.2+7*18)=5.2pt。
    frames = {"summary": (243.7, 25.2, 61.9, 272.8, 3),
              "education": (339.05, 26.25, 79.0, 369.2, 4),
              "work": (451.75, 29.4, 229.2, 485.2, 12),
              "skills": (720.15, 26.2, 61.9, 750.4, 3)}
    probe = {"frames": {}, "paragraphs": {}, "order": list(frames)}
    for key, (top, offset, height, ink_top, lines) in frames.items():
        probe["frames"][key] = {"frame_top_pt": top, "body_wsp_off_y_pt": offset, "body_h_pt": height}
        probe["paragraphs"][key] = [{"top_pt": ink_top, "lines": lines, "text": key}]
    probe["paragraphs"]["work"] = [
        {"top_pt": 485.2, "lines": 7, "text": "work1"},
        {"top_pt": 611.2, "lines": 0, "text": ""},
        {"top_pt": 616.4, "lines": 5, "text": "work2"},
    ]
    archive = spacing.build_archive_from_probe(
        probe, template_id="t001", template_sha256=sha256_file(template), title_text_top_off_pt=3.9)
    spacing.save_archive(archive, path)
    return archive
