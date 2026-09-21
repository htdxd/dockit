"""生产用组件测量与 DOCX 生成；证据场景留在 generate.py。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from lxml import etree

from skill_toolbox.resume_layout import emit, layout, measure, typography
from skill_toolbox.resume_layout.layout import MeasureResult
from skill_toolbox.resume_layout.header import apply_header_components, has_header_edits
from skill_toolbox.resume_layout.t109 import (
    SOURCE_SECTION_NAMES, MASTER_PHOTO_ANCHOR_NAME,
    MEASURE_BODY_ITEM_INDEX, FALLBACK_PROTO_SECTION,
)

def measure_scenario(scenario: dict, work_dir: Path, *, template: Path, template_id: str = "t109") -> dict[str, MeasureResult]:
    """复制原件为测量宿主 → COM 批量测量所有条目。

    逐栏目测量：每栏条目写入**本栏目自己的正文框**（样式档案不同——
    实习/校园职责段带项目符号、宽度缩进不同会影响 wrap），单实例批量。
    """
    source_root = emit.load_document_xml(template)
    hosts = []
    for sec in scenario["sections"]:
        if template_id == "t001":
            from skill_toolbox.resume_layout import t001
            source_id = t001.SECTIONS[t001.source_key(sec)][1]
            source = t001.anchor_by_id(source_root, source_id)
        else:
            source_id = sec["id"] if sec["id"] in SOURCE_SECTION_NAMES else FALLBACK_PROTO_SECTION
            source = emit._anchor_by_docpr_name(source_root, SOURCE_SECTION_NAMES[source_id])
        source_dp_id = source.find(emit.WP + "docPr").get("id")
        roles = emit.build_style_roles(emit.find_body_wsp(source).find(".//" + emit.W + "txbxContent"))
        for entry in sec["entries"]:
            root = copy.deepcopy(source_root)
            anchor = next(a for a in root.iter(emit.WP + "anchor")
                          if a.find(emit.WP + "docPr").get("id") == source_dp_id)
            anchor.find(emit.WP + "docPr").set("name", "ResumeMeasureBody")
            body = emit.find_body_wsp(anchor)
            typography.scale_title(anchor, float(sec.get("scale") or 1),
                                    body=body, template_id=template_id)
            emit.set_box_text(body, entry["text"], style_roles=roles,
                              first_is_header=entry.get("has_heading"), header_lines=entry.get("heading_lines"),
                              tech_stack_line=entry.get("tech_stack_line"), detail_styles=entry.get("detail_styles"), inline_styles=entry.get("inline_styles"), paragraph_styles=entry.get("paragraph_styles"))
            numbering = typography.Numbering(template)
            metrics = typography.apply_body(body, entry, sec, template_id, numbering)
            if template_id == "t001":
                anchor.find(emit.WP + "extent").set("cx", str(emit.pt2emu(metrics["body_width_pt"])))
            pv = anchor.find(emit.WP + "positionV")
            top = int(pv.find(emit.WP + "posOffset").text) / emit.EMU
            top += 72 if pv.get("relativeFrom") == "paragraph" else 0
            if template_id != "t001":
                top += metrics["body_offset_pt"]
            host = work_dir / f"measure-{len(hosts)}.docx"
            emit.save_document_xml(root, template, host, part_overrides=numbering.parts() or None)
            hosts.append({"id": entry["id"], "host": host.resolve(),
                          **metrics, "body_top_pt": top,
                          "body_item": None if template_id == "t001" else MEASURE_BODY_ITEM_INDEX})
    all_results = measure.measure_documents(hosts)
    header = scenario.get("header") or {}
    if has_header_edits(header):
        if template_id == "t001":
            from skill_toolbox.resume_layout import t001
            fit = t001.fit_header(template, header.get("fields") or {}, work_dir, header=header)
        else:
            from skill_toolbox.resume_layout import t109
            fit = t109.fit_header(template, header, work_dir)
        (work_dir / "header_fit.json").write_text(json.dumps(fit), encoding="utf-8")
    (work_dir / "measure_result.json").write_text(
        json.dumps(
            {"ok": True, "results": [r.to_dict() for r in all_results]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return {
        r.entry_id: MeasureResult.from_dict(r.to_dict())
        for r in all_results
    }


# ---------------- 落盘阶段 ----------------

def emit_scenario(
    scenario: dict, plan: layout.LayoutPlan, out_docx: Path, *, template: Path, template_id: str = "t109"
) -> dict:
    """在原件副本上：克隆/重定位栏目、写文本、真实分页。

    scenario 可选字段（P2 修订）：
    - `header.fields`：母版个人信息区「标签：值」替换（键为 name/phone/…）
    - `header.photo_bytes` + `header.photo_part`：替换照片媒体部件（等比适配）
    - 条目级 `width_pt`：正文框宽度（由布局层按真实测量决定，变窄则多换行）
    """
    if template_id == "t001":
        from skill_toolbox.resume_layout import t001
        return t001.emit_scenario(scenario, plan, out_docx, template=template)
    root = emit.load_document_xml(template)
    numbering = typography.Numbering(template)
    body = root.find(emit.W + "body")
    paragraphs = body.findall(emit.W + "p")

    # 收集原件 5 个栏目 anchor，并先深拷贝整个 AlternateContent 作克隆源
    # （后续原件栏目会被移除，原型必须先快照）。
    # **按栏目各自克隆**：图标 custGeom、横线、正文段落样式（粗体/numPr
    # 项目符号）在 5 个栏目间各不相同——克隆源必须用栏目自己的原型，
    # 才能保留视觉风格（review Fix4）。
    source_anchor_by_section = {}
    proto_snapshot: dict[str, etree._Element] = {}
    for sid, name in SOURCE_SECTION_NAMES.items():
        a = emit._anchor_by_docpr_name(root, name)
        if a is None:
            raise RuntimeError(f"找不到源栏目 {name}")
        source_anchor_by_section[sid] = a
        node = a
        while node is not None and etree.QName(node).localname != "AlternateContent":
            node = node.getparent()
        if node is None:
            raise RuntimeError(f"源栏目 {name} 缺 AlternateContent")
        proto_snapshot[sid] = copy.deepcopy(node)

    # 隐藏原件 5 个栏目（整块 drawing 从 AlternateContent 移除），
    # 只留母版（顶部色带/底部条/个人信息/照片）
    for sid, anchor in source_anchor_by_section.items():
        node = anchor
        while node is not None and etree.QName(node).localname != "AlternateContent":
            node = node.getparent()
        host_run = node.getparent()
        host_run.getparent().remove(host_run)

    # 每个栏目挂回自己的原型（克隆源 anchor）；模板没有的栏目
    # （如 projects）复用实习栏原型。
    proto_anchor_by_section: dict[str, etree._Element] = {}
    needed_sections = [s["id"] for s in scenario["sections"]]
    for sid in needed_sections:
        src = sid if sid in proto_snapshot else FALLBACK_PROTO_SECTION
        proto_anchor_by_section[sid] = emit.insert_proto(root, copy.deepcopy(proto_snapshot[src]))

    # 承载段落：最后一个空段落（p2..p8 中选最后一个 body 段）；真实分页符注入
    # 分页机制：每页一个承载段落；页与页之间插入「独立分页段落」
    # （段落内仅一个 <w:br w:type="page"/> run）。分页符不能与 anchor 同段
    # ——Word 会把同段 anchor 视为分页前内容（实测重叠）。
    carrier_by_page: dict[int, etree._Element] = {}
    p_first = paragraphs[2]
    carrier_by_page[0] = p_first
    prev_carrier = p_first
    for page in range(1, plan.pages):
        brk_p = etree.Element(emit.W + "p")
        r = etree.SubElement(brk_p, emit.W + "r")
        br = etree.SubElement(r, emit.W + "br")
        br.set(emit.W + "type", "page")
        prev_carrier.addnext(brk_p)
        carrier_p = etree.Element(emit.W + "p")
        brk_p.addnext(carrier_p)
        prev_carrier = carrier_p
        carrier_by_page[page] = carrier_p

    # 原件装饰锚定在末尾段落；插入分页后必须逐页重新挂载。
    # 个人信息和照片仍只出现一次，只有顶部色带/底条重复。
    for name in ("矩形 213", "组合 3"):
        anchor = emit._anchor_by_docpr_name(root, name)
        if anchor is None:
            raise RuntimeError(f"找不到母版装饰 {name}")
        source = anchor
        while etree.QName(source).localname != "AlternateContent":
            source = source.getparent()
        source.getparent().remove(source)
        for page, carrier in carrier_by_page.items():
            decoration = source if page == 0 else copy.deepcopy(source)
            decorated_anchor = decoration.find(".//" + emit.WP + "anchor")
            if page:
                next_id = max(emit._scan_max(root, "docPr", "id"),
                              emit._scan_max(root, "cNvPr", "id")) + 1
                emit.uniquify_ids(root, decorated_anchor, next_id)
            etree.SubElement(carrier, emit.W + "r").append(decoration)

    # 逐栏目克隆放置（用栏目自己的原型 → 图标/横线/样式随栏目保留）
    box_records = []
    emit_records = []
    for sec in plan.sections:
        scen_sec = next(s for s in scenario["sections"] if s["id"] == sec.section_id)
        proto_anchor = proto_anchor_by_section[sec.section_id]
        # 从原型正文框按**角色**分桶样式（P2-1：条目标题段 / 职责段，
        # 由 numPr 结构判定，不再按段落序号套用——否则新增职责行会继承
        # 第 2 条经历的标题样式，渲染成加粗且无项目符号）
        proto_body = emit.find_body_wsp(proto_anchor)
        style_roles = emit.build_style_roles(
            proto_body.find(".//" + emit.W + "txbxContent")
        )
        for k, entry_plan in enumerate(sec.entries):
            new_anchor = emit.clone_section_anchor(
                root, proto_anchor, title_text=scen_sec["title"] if k == 0 else ""
            )
            # 克隆链：anchor > drawing > Choice > AlternateContent > r
            node = new_anchor
            while node is not None and etree.QName(node).localname != "AlternateContent":
                node = node.getparent()
            if node is None:
                raise RuntimeError("克隆 anchor 缺 AlternateContent 祖先")
            ac = node
            ac.getparent().remove(ac)  # 先脱离原宿主 run
            carrier = carrier_by_page[entry_plan.page_index]
            run = etree.SubElement(carrier, emit.W + "r")
            run.append(ac)
            # posV：k=0 的克隆是「标题组合」（标题框在 anchor 顶 0~30.2，
            # 正文框在 anchor 内 off 29.2），放栏目标题 y；k>0 的克隆已剥掉
            # 标题/装饰，只剩正文框（anchor 内 off 29.2），布局给的
            # entry anchor_y 已按正文框顶计算，直接使用。
            if k == 0:
                emit.set_anchor_pos_v(new_anchor, sec.anchor_y_pt)
            else:
                emit.set_anchor_pos_v(new_anchor, entry_plan.anchor_y_pt -
                                      (entry_plan.body_offset_pt or 29.2))
            # 正文框：写文本（按原型段落样式快照，保留 numPr/粗体模式）+ 全链伸展
            entry_text = next(
                e["text"] for e in scen_sec["entries"] if e["id"] == entry_plan.instance_id
            )
            body_wsp = emit.find_body_wsp(new_anchor)
            typography.scale_title(new_anchor, float(scen_sec.get("scale") or 1),
                                    body=body_wsp, template_id=template_id)
            has_heading = next(e.get("has_heading") for e in scen_sec["entries"]
                               if e["id"] == entry_plan.instance_id)
            entry_data = next(e for e in scen_sec["entries"] if e["id"] == entry_plan.instance_id)
            emit.set_box_text(body_wsp, entry_text, style_roles=style_roles,
                              first_is_header=has_heading, header_lines=entry_data.get("heading_lines"),
                              tech_stack_line=entry_data.get("tech_stack_line"), detail_styles=entry_data.get("detail_styles"), inline_styles=entry_data.get("inline_styles"), paragraph_styles=entry_data.get("paragraph_styles"))
            typography.apply_body(body_wsp, entry_data, scen_sec, template_id, numbering)
            emit.resize_group_child_bottom(new_anchor, body_wsp, entry_plan.body_h_pt)
            # 宽度覆盖：布局按真实测量给出的宽度（≤ 模板正文宽）；测量侧
            # 用同一宽度，保证「换行行数」与产物一致
            if k > 0:
                emit.strip_title_decorations(new_anchor)
            box_records.append({
                "section_id": f"{sec.section_id}[{k}]",
                "page_index": entry_plan.page_index,
                "top_pt": entry_plan.anchor_y_pt,
                "bottom_pt": entry_plan.anchor_y_pt + entry_plan.body_h_pt,
            })
            emit_records.append({
                "instance_id": entry_plan.instance_id,
                "page": entry_plan.page_index,
                "anchor_y_pt": entry_plan.anchor_y_pt,
                "body_h_pt": entry_plan.body_h_pt,
            })
    # 删除全部克隆源 proto 的宿主段落（其 anchor 是模板原文，不能进入产物）
    for sid, proto_anchor in proto_anchor_by_section.items():
        proto_host_p = proto_anchor.getparent()
        while proto_host_p is not None and etree.QName(proto_host_p).localname != "p":
            proto_host_p = proto_host_p.getparent()
        if proto_host_p is None:
            raise RuntimeError(f"proto({sid}) 宿主段落定位失败")
        proto_host_p.getparent().remove(proto_host_p)
    # 个人信息与照片（母版）：字段替换 + 照片媒体替换（图片等比适配）
    header = scenario.get("header") or {}
    from skill_toolbox.resume_layout.t109 import header_boxes
    header_record: dict = {
        "fields": apply_header_components(header_boxes(root), emit.INFO_FIELD_LABELS, header),
        "fit": header.get("fit") or {},
    }
    part_overrides: dict[str, bytes] = numbering.parts()
    if header.get("hide_photo"):
        photo_anchor = emit._anchor_by_docpr_name(root, MASTER_PHOTO_ANCHOR_NAME)
        node = photo_anchor
        while emit.etree.QName(node).localname != "AlternateContent":
            node = node.getparent()
        node.getparent().remove(node)
    elif header.get("photo_bytes"):
        photo_anchor = emit._anchor_by_docpr_name(root, MASTER_PHOTO_ANCHOR_NAME)
        if photo_anchor is None:
            raise RuntimeError(f"找不到照片 anchor {MASTER_PHOTO_ANCHOR_NAME}")
        header_record["photo"] = emit.replace_photo(photo_anchor, header["photo_bytes"])
        header_record["photo"]["part"] = header.get("photo_part", "")
        part_overrides[header["photo_part"]] = header["photo_bytes"]

    photo_y = (header.get("fit") or {}).get("photo_y_pt")
    if photo_y is not None and not header.get("hide_photo"):
        emit.set_anchor_pos_v(emit._anchor_by_docpr_name(root, MASTER_PHOTO_ANCHOR_NAME), photo_y)
        header_record.setdefault("photo", {})["top_pt"] = photo_y

    # 保存
    tmp_out = out_docx.with_suffix(".docx.tmp")
    emit.save_document_xml(root, template, tmp_out, part_overrides=part_overrides or None)
    tmp_out.replace(out_docx)
    return {
        "boxes": box_records,
        "emit_records": emit_records,
        "pages": plan.pages,
        "header": header_record,
    }



def main() -> None:
    """受控子进程入口：测量正文并建立/复用原件间距档案。"""
    import sys
    from skill_toolbox.resume_layout import spacing

    template, spec, work, archive = map(Path, sys.argv[1:5])
    template_id = sys.argv[5] if len(sys.argv) > 5 else "t109"
    if str(spec) != "-":
        measure_scenario(json.loads(spec.read_text(encoding="utf-8")), work,
                         template=template, template_id=template_id)
    if template_id == "t001":
        from skill_toolbox.resume_layout import t001
        t001.ensure_archive(template, archive)
    else:
        spacing.ensure_archive(template, archive)


if __name__ == "__main__":
    main()
