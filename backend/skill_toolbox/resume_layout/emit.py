# -*- coding: utf-8 -*-
"""emit.py — LayoutPlan → DOCX（t109 探针）。

核心几何操作（全部在副本上执行，原件只读）：
1. 栏目（标题+图标+横线+正文框组合）整组克隆：deepcopy drawing（含 mc:AlternateContent
   Choice/Fallback 同步），赋唯一 docPr id / anchorId / relativeHeight。
2. 组内子对象只改 body wsp 的 ext.cy（正文本体）；标题框/图标/横线不动。
3. 全链组伸展：wsp ext → 嵌套 grpSp chExt/ext → wgp chExt/ext → anchor extent。
4. 页面放置：anchor posV 写目标 y；第二页用「真实分页符段落」承载——
   在 body 末尾插入分页 run，克隆的 drawing 挂到分页后的段落 run 上，
   posV 用 page 基准（每页相同坐标域）。
5. 条目文本写入：克隆的正文框 txbxContent 整体替换，段落样式**按角色**复用
   （P2-1：条目标题段 / 职责段，由 numPr 结构判定；不按段落序号套用——
   序号式取样会把新增职责行映射到第 2 条经历的标题样式）。

坐标语义（P0 基线锚定）：anchor posV 相对 page；同一 posV 在第 1/2 页含义相同。
"""
from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path

from lxml import etree

EMU = 12700
# 标题框高下界（pt）：用于结构识别「组合顶部、高≈30.2 的标题文本框」
TITLE_BOX_MIN_H_PT = 25.0
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
WPG = "{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}"


def pt2emu(v: float) -> int:
    return int(round(v * EMU))


def emu2pt(v: int | str) -> float:
    return int(v) / EMU


# ---------------- 包读写 ----------------

def load_document_xml(src: Path) -> etree._Element:
    with zipfile.ZipFile(src) as z:
        return etree.fromstring(z.read("word/document.xml"))


def save_document_xml(
    root,
    src: Path,
    out: Path,
    *,
    part_overrides: dict[str, bytes] | None = None,
) -> None:
    """写回 document.xml；`part_overrides` 可替换/新增其它部件（如照片媒体）。

    模板原件只读：所有替换都发生在输出副本上。
    """
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    tmp = out.with_suffix(".docx.tmp")
    overrides = dict(part_overrides or {})
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/document.xml":
                zout.writestr(item, xml)
            elif item.filename in overrides:
                zout.writestr(item, overrides.pop(item.filename))
            else:
                zout.writestr(item, zin.read(item.filename))
        for name, data in overrides.items():  # 新增部件（模板中不存在的媒体等）
            zout.writestr(name, data)
    tmp.replace(out)


# ---------------- 定位 ----------------

def _anchors(root) -> list:
    return root.findall(".//" + WP + "anchor")


def _anchor_by_docpr_name(root, name: str):
    for a in _anchors(root):
        dp = a.find(WP + "docPr")
        if dp is not None and dp.get("name") == name:
            return a
    return None


def _box_text(wsp) -> str:
    tx = wsp.find(".//" + W + "txbxContent")
    if tx is None:
        return ""
    return "\n".join(
        "".join(t.text or "" for t in p.iter(W + "t")) for p in tx.iter(W + "p")
    )


def _find_body_wsp(anchor, probe_text: str):
    for wsp in anchor.iter(WPS + "wsp"):
        if probe_text in _box_text(wsp):
            return wsp
    return None


# ---------------- 文本写入（保样式：段落级快照） ----------------

def _has_numpr(p) -> bool:
    ppr = p.find(W + "pPr")
    return ppr is not None and ppr.find(W + "numPr") is not None


def _para_style_snapshots(tx) -> list[dict]:
    """快照框内每段的样式：pPr 深拷贝 + 首 run rPr 深拷贝 + numPr 事实。

    保留 numPr（项目符号）、对齐、缩进、粗体、颜色等全部段落级事实。
    **空段（无文字）跳过**——模板常用小字号空段做条目间视觉分隔（如实习栏
    p3 为 sz=6 的 3pt 空段），它是分隔符不是内容样式模板；内容行落到空段
    样式会产生 13.5pt 异常行距（R3 review 的 8.08pt 高度差根源）。
    """
    snaps = []
    for p in tx.iter(W + "p"):
        text = "".join(t.text or "" for t in p.iter(W + "t"))
        if not text.strip():
            continue
        ppr = p.find(W + "pPr")
        rpr = None
        for r in p.iter(W + "r"):
            cand = r.find(W + "rPr")
            if cand is not None:
                rpr = cand
            break  # 只取首 run
        snaps.append({
            "pPr": copy.deepcopy(ppr) if ppr is not None else None,
            "rPr": copy.deepcopy(rpr) if rpr is not None else None,
            "has_numpr": _has_numpr(p),
        })
    return snaps


# ---------------- 段落角色（P2-1：不按段落序号套样式） ----------------

ROLE_HEADER = "entry_header"
ROLE_DUTY = "entry_duty"

# 条目标题行判据：日期区间（2012.06-至今 / 2005.07-2009.06）或列对齐
# （机构/角色以 2+ 个空白分列）。只用于选角色样式，不改变任何文本内容。
_HEADER_DATE_RE = re.compile(r"\d{4}\s*[年./\-]\s*\d{1,2}")
_HEADER_COL_RE = re.compile(r"\S[ \u3000]{2,}\S")


def looks_like_entry_header(line: str) -> bool:
    """条目标题行（首行）判定：含日期区间，或含 ≥2 段列对齐空白。"""
    if _HEADER_DATE_RE.search(line):
        return True
    return len(_HEADER_COL_RE.findall(line)) >= 2


def build_style_roles(tx) -> dict[str, list[dict]]:
    """把原型框的段落样式**按角色**分桶：条目标题 / 职责正文。

    t109 原件事实（P2-1 实测，五栏逐段核对）：
    - 条目标题段：无 `numPr`，`rPr` 带 `<w:b/>`（粗体，如实习 p0/p4、教育 p0）；
    - 职责段：`w:pStyle=4` + `w:numPr` + `ind firstLineChars=0`（项目符号）；
    - 教育/技能/自我评价栏正文框**无 numPr**：标题角色 = 首段，职责角色 = 其余段。

    角色按结构（numPr 有无）判定，**不按段落序号**——R3 review 的 P2 缺陷
    （grow 新增职责段继承第 2 条经历标题样式 → 加粗且丢项目符号）即源于
    跳过空段后按位置取快照：原件 7 段 → 快照 [头,职责,职责,头,职责,职责]，
    第 4 个内容行落到 `头`。角色分桶后新增职责行永远取职责段样式。
    """
    snaps = _para_style_snapshots(tx)
    if not snaps:
        raise RuntimeError("源框无段落样式快照（空原型？）")
    duty = [s for s in snaps if s["has_numpr"]]
    header = [s for s in snaps if not s["has_numpr"]]
    if not duty:
        # 无 numPr 的正文框：标题=首段，职责=其余段（逐位保留原样式差异）
        header = snaps[:1]
        duty = snaps[1:] or snaps[:1]
    if not header:
        # 全框都是 numPr（无独立标题段）：标题回落到职责段
        header = duty[:1]
    return {ROLE_HEADER: header, ROLE_DUTY: duty}


def set_box_text(
    wsp,
    new_text: str,
    *,
    style_roles: dict[str, list[dict]] | None = None,
    style_snaps: list[dict] | None = None,
    first_is_header: bool | None = None,
    header_lines: int | None = None,
    tech_stack_line: int | None = None,
) -> None:
    """整框替换文本，**按段落角色复用源样式**（P2-1）。

    style_roles: 由 `build_style_roles(源框 txbxContent)` 得到的
        `{ROLE_HEADER: [...], ROLE_DUTY: [...]}`；缺省时对当前框自身分桶。
        第 0 行若判定为条目标题（日期/列对齐）→ header 角色，否则 duty；
        第 i>0 行 → duty 角色（超出桶长时沿用最后一个职责段样式）。
    style_snaps: 兼容参数（旧调用/单测）——按段落位置取样式，不给角色语义。
    """
    tx = wsp.find(".//" + W + "txbxContent")
    if tx is None:
        raise RuntimeError("目标 wsp 无 txbxContent")
    if style_roles is None and style_snaps is None:
        style_roles = build_style_roles(tx)
    if style_roles is not None:
        head = style_roles.get(ROLE_HEADER) or style_roles[ROLE_DUTY]
        duty = style_roles[ROLE_DUTY]
        if not duty or not head:
            raise RuntimeError("角色样式桶为空（空原型？）")

        def snap_for(i: int, line: str) -> dict:
            if header_lines is not None:
                duty_index = i - header_lines - int(tech_stack_line is not None and i > tech_stack_line)
                return head[0] if i < header_lines else duty[min(duty_index, len(duty) - 1)]
            if i == 0 and (first_is_header if first_is_header is not None else looks_like_entry_header(line)):
                return head[min(0, len(head) - 1)]
            return duty[min(max(i - 1, 0), len(duty) - 1)]
    else:
        if style_snaps is None or not style_snaps:
            raise RuntimeError("源框无段落样式快照（空原型？）")

        def snap_for(i: int, line: str) -> dict:
            return style_snaps[min(i, len(style_snaps) - 1)]

    paras = list(tx.iter(W + "p"))
    first_p = paras[0]
    lines = new_text.split("\n")
    # 清掉除首段外的全部段落
    for p in paras[1:]:
        tx.remove(p)
    for r in list(first_p.iter(W + "r")):
        first_p.remove(r)
    prev_p = first_p
    for i, line in enumerate(lines):
        snap = snap_for(i, line)
        if i == 0:
            p = first_p
            # 重挂快照 pPr（含 numPr/缩进/对齐）
            old_ppr = p.find(W + "pPr")
            if old_ppr is not None:
                p.remove(old_ppr)
            if snap["pPr"] is not None:
                p.insert(0, copy.deepcopy(snap["pPr"]))
        else:
            p = etree.SubElement(tx, W + "p")
            if snap["pPr"] is not None:
                p.append(copy.deepcopy(snap["pPr"]))
            prev_p.addnext(p)
        if i == tech_stack_line:
            props = p.find(W + "pPr")
            if props is not None:
                for tag in ("numPr", "ind"):
                    child = props.find(W + tag)
                    if child is not None:
                        props.remove(child)
        r = etree.SubElement(p, W + "r")
        if snap["rPr"] is not None:
            r.append(copy.deepcopy(snap["rPr"]))
        for index, chunk in enumerate(line.split("\t")):
            if index:
                etree.SubElement(r, W + "tab")
            t = etree.SubElement(r, W + "t")
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t.text = chunk
        prev_p = p


# ---------------- 组几何（全链伸展） ----------------

def _xfrm_of_group(grp):
    gpr = grp.find(WPG + "grpSpPr")
    return gpr.find(A + "xfrm") if gpr is not None else None


def set_child_width(wsp, cx_pt: float) -> None:
    """只改正文框宽度（ext.cx）。

    宽度由布局层按真实测量决定：标题/图标/横线仍按模板位置摆放，正文框变窄
    只影响换行。故意**不动组链 cx**——组整体尺寸保持模板值，避免把标题横线
    一起拉伸。宽度校验（上限=模板正文宽）在调用方完成。
    """
    sp = wsp.find(WPS + "spPr")
    xf = sp.find(A + "xfrm")
    ext = xf.find(A + "ext")
    ext.set("cx", str(pt2emu(cx_pt)))


# ---------------- 个人信息（母版文本框）与照片 ----------------

# 模板母版个人信息区的标签 → 内容键（标签按去空白后匹配；值原样替换）
INFO_FIELD_LABELS: dict[str, str] = {
    "姓名": "name",
    "民族": "ethnicity",
    "电话": "phone",
    "邮箱": "email",
    "住址": "address",
    "出生年月": "birth",
    "身高": "height",
    "政治面貌": "politics",
    "毕业院校": "school",
    "学历": "degree",
}


def _split_label_value(text: str) -> tuple[str, str] | None:
    for sep in ("：", ":"):
        if sep in text:
            label, value = text.split(sep, 1)
            return label, value
    return None


def set_info_fields(anchor, fields: dict[str, str], *,
                    labels: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """替换母版个人信息区「标签：值」段落的值，保留标签与字体样式。

    返回 `{key: {"label":…, "before":…, "after":…}}`（供交付报告核对）。
    未识别的键由调用方先行拒绝（不做静默忽略）。
    """
    applied: dict[str, dict[str, str]] = {}
    for wsp in anchor.iter(WPS + "wsp"):
        tx = wsp.find(".//" + W + "txbxContent")
        if tx is None:
            continue
        for p in tx.iter(W + "p"):
            runs = [r for r in p.iter(W + "r")]
            if not runs:
                continue
            texts = [
                "".join(t.text or "" for t in r.iter(W + "t")) for r in runs
            ]
            joined = "".join(texts)
            split = _split_label_value(joined)
            if split is None:
                continue
            label_raw, value = split
            key = (labels or INFO_FIELD_LABELS).get("".join(label_raw.split()))
            if key is None or key not in fields:
                continue
            new_value = str(fields[key])
            # 定位「含冒号」的 run：标签留在该 run，值写进其后第一个 run
            colon_idx = None
            for i, text in enumerate(texts):
                if "：" in text or ":" in text:
                    colon_idx = i
                    break
            if colon_idx is None:
                continue
            sep = "：" if "：" in texts[colon_idx] else ":"
            label_part = texts[colon_idx].split(sep, 1)[0] + sep
            if colon_idx + 1 == len(runs):
                value_run = copy.deepcopy(runs[colon_idx])
                runs[colon_idx].addnext(value_run)
                runs.append(value_run)
            value_run = runs[colon_idx + 1]
            _set_run_text(runs[colon_idx], label_part)
            _set_run_text(value_run, new_value)
            for r in runs[colon_idx + 2:]:
                _set_run_text(r, "")
            applied[key] = {"label": label_raw.strip(), "before": value, "after": new_value}
    return applied


def _set_run_text(run, text: str) -> None:
    ts = list(run.iter(W + "t"))
    if not ts:
        t = etree.SubElement(run, W + "t")
        ts = [t]
    ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    ts[0].text = text
    for extra in ts[1:]:
        extra.getparent().remove(extra)


def replace_photo(anchor, image_bytes: bytes, *, frame_cx_pt: float | None = None,
                  frame_cy_pt: float | None = None) -> dict:
    """只调整组内照片，补偿父组缩放；保留外框并等比居中，不裁切。"""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        img_w, img_h = im.size
    pic_ns = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
    picture = anchor.find(".//" + pic_ns + "pic")
    transform = picture.find(pic_ns + "spPr/" + A + "xfrm")
    ext, off = transform.find(A + "ext"), transform.find(A + "off")
    scale_x = scale_y = 1.0
    for parent in picture.iterancestors():
        group = parent.find(WPG + "grpSpPr/" + A + "xfrm")
        if group is not None:
            outer, inner = group.find(A + "ext"), group.find(A + "chExt")
            scale_x *= int(outer.get("cx")) / int(inner.get("cx"))
            scale_y *= int(outer.get("cy")) / int(inner.get("cy"))
    cx_pt = frame_cx_pt or emu2pt(ext.get("cx")) * scale_x
    cy_pt = frame_cy_pt or emu2pt(ext.get("cy")) * scale_y
    fit = min(cx_pt / img_w, cy_pt / img_h)
    new_cx, new_cy = img_w * fit, img_h * fit
    off_x, off_y = (cx_pt - new_cx) / 2.0, (cy_pt - new_cy) / 2.0
    ext.set("cx", str(pt2emu(new_cx / scale_x)))
    ext.set("cy", str(pt2emu(new_cy / scale_y)))
    off.set("x", str(int(off.get("x")) + pt2emu(off_x / scale_x)))
    off.set("y", str(int(off.get("y")) + pt2emu(off_y / scale_y)))
    fill = picture.find(pic_ns + "blipFill")
    crop = fill.find(A + "srcRect")
    if crop is not None:
        fill.remove(crop)
    return {
        "width_pt": round(new_cx, 2), "height_pt": round(new_cy, 2),
        "off_x_pt": round(off_x, 2), "off_y_pt": round(off_y, 2),
        "resized": (round(new_cx, 2), round(new_cy, 2)) != (round(cx_pt, 2), round(cy_pt, 2)),
    }


def resize_group_child_bottom(anchor, wsp, new_child_cy_pt: float) -> None:
    """把 anchor 组合内 wsp 的 ext.cy 设为 new_child_cy_pt，并同步整条组链。

    t109 结构（如 anchor 2）：
      anchor > graphic > graphicData > wgp > grpSp(outer) > grpSp(inner) > [wsp...]
    各层 xfrm 的 off/ext 是父空间坐标，chOff/chExt 是子空间坐标系。
   伸展策略（保持 scale=1）：子内容底部 → 逐层重算 ext.cy 与 chExt.cy。
    """
    sp = wsp.find(WPS + "spPr")
    xf = sp.find(A + "xfrm")
    off = xf.find(A + "off")
    ext = xf.find(A + "ext")
    child_bottom = emu2pt(off.get("y")) + new_child_cy_pt
    ext.set("cy", str(pt2emu(new_child_cy_pt)))
    # wgp 的 chOff/chExt：t109 为 identity（0,0 / ext），子底部即 chExt 底
    wgp = wsp.getparent()
    while wgp is not None and etree.QName(wgp).localname != "wgp":
        wgp = wgp.getparent()
    if wgp is None:
        # 无组（独立 anchor）——只调 anchor extent
        aext = anchor.find(WP + "extent")
        aext.set("cy", str(pt2emu(child_bottom)))
        return
    # wgp 直接子层（可能是 grpSp 或 wsp 列表）——重算 wgp 子空间包围盒
    wxf = _xfrm_of_group(wgp)
    wch_off = wxf.find(A + "chOff")
    wch_ext = wxf.find(A + "chExt")
    new_cy = child_bottom - emu2pt(wch_off.get("y"))
    wch_ext.set("cy", str(pt2emu(new_cy)))
    wxf.find(A + "ext").set("cy", str(pt2emu(new_cy)))
    # anchor extent 同步（wgp off 一般为 0,0 → ext.cy = new_cy + off.y）
    woff = wxf.find(A + "off")
    aext = anchor.find(WP + "extent")
    aext.set("cy", str(pt2emu(new_cy + emu2pt(woff.get("y")))))


def set_anchor_pos_v(anchor, y_pt: float) -> None:
    pv = anchor.find(WP + "positionV/" + WP + "posOffset")
    if pv is None or pv.text is None:
        raise RuntimeError("anchor 缺 positionV/posOffset")
    pv.text = str(pt2emu(y_pt))


# ---------------- ID 唯一化 ----------------

def _scan_max(root, localname: str, attr: str) -> int:
    mx = 0
    for el in root.iter():
        if etree.QName(el).localname == localname:
            v = el.get(attr)
            if v and v.isdigit():
                mx = max(mx, int(v))
    return mx


def uniquify_ids(root, anchor, start_id: int) -> int:
    """为克隆的 anchor 分配唯一 docPr id / cNvPr id / anchorId / relativeHeight。"""
    next_id = start_id
    dp = anchor.find(WP + "docPr")
    if dp is not None:
        dp.set("id", str(next_id))
        dp.set("name", f"{dp.get('name', 'Component')} Clone {next_id}")
    next_id += 1
    for el in anchor.iter():
        ln = etree.QName(el).localname
        if ln == "cNvPr" and el.get("id", "").isdigit():
            el.set("id", str(next_id))
            next_id += 1
        for attr in list(el.attrib):
            if etree.QName(attr).localname in ("anchorId", "editId"):
                el.set(attr, f"{next_id:08X}")
                next_id += 1
    rhs = [
        int(a.get("relativeHeight"))
        for a in root.iter(WP + "anchor")
        if (a.get("relativeHeight") or "").isdigit()
    ]
    anchor.set("relativeHeight", str(max(rhs, default=0) + 1))
    return next_id


# ---------------- 克隆栏目 ----------------

def insert_proto(root, proto_ac) -> etree._Element:
    """把快照的 AlternateContent 原型挂回 body（生成克隆源 anchor）。

    w:r 必须包在 w:p 里且置于 sectPr 之前（body 直接子级只允许 p/sectPr）。
    """
    body = root.find(W + "body")
    p = etree.SubElement(body, W + "p")
    host_run = etree.SubElement(p, W + "r")
    host_run.append(proto_ac)
    sect = body.find(W + "sectPr")
    if sect is not None:
        body.remove(p)
        sect.addprevious(p)  # 保持 sectPr 最后
    return proto_ac.find(".//" + WP + "anchor")


def clone_section_anchor(root, source_anchor, *, title_text: str) -> etree._Element:
    """深拷贝源栏目的整个 drawing（AlternateContent 一并拷贝），重写唯一 ID。

    返回克隆 anchor（已在文档中，插入位置由调用方决定）。
    标题文字写入克隆的标题框；正文框清空待填。
    """
    drawing = source_anchor.getparent()
    while drawing is not None and etree.QName(drawing).localname != "drawing":
        drawing = drawing.getparent()
    if drawing is None:
        raise RuntimeError("源栏目 anchor 不在 w:drawing 内")
    # drawing 父链：Choice > AlternateContent > r；克隆整个 AlternateContent
    ac = drawing.getparent()
    while ac is not None and etree.QName(ac).localname != "AlternateContent":
        ac = ac.getparent()
    if ac is None:
        raise RuntimeError("源栏目 drawing 不在 mc:AlternateContent 内")
    host_run = ac.getparent()          # w:r
    new_ac = copy.deepcopy(ac)
    new_anchor = new_ac.find(".//" + WP + "anchor")
    if new_anchor is None:
        raise RuntimeError("克隆 AlternateContent 内无 anchor")
    host_run.addnext(new_ac)
    start = _scan_max(root, "docPr", "id") + 100
    uniquify_ids(root, new_anchor, start)
    # 标题框写入新标题（结构识别：组合顶部 off.y≈0、高≈30.2 的文本框；
    # 不依赖标题文本是否含全角括号——见 find_title_wsp 的说明）
    title_wsp = find_title_wsp(new_anchor)
    if title_wsp is not None:
        set_box_text(title_wsp, title_text)
    return new_anchor


def _box_geom(wsp) -> tuple[float, float]:
    """文本框在 anchor 组合内的 (off_y_pt, cy_pt)；缺几何返回 (0, 0)。"""
    sp = wsp.find(WPS + "spPr")
    xf = sp.find(A + "xfrm") if sp is not None else None
    off = xf.find(A + "off") if xf is not None else None
    ext = xf.find(A + "ext") if xf is not None else None
    off_y = emu2pt(off.get("y")) if off is not None and off.get("y") else 0.0
    cy = emu2pt(ext.get("cy")) if ext is not None and ext.get("cy") else 0.0
    return off_y, cy


def find_title_wsp(anchor):
    """标题框（结构识别）：位于组合顶部（off.y≈0）且高度≈标题框高的文本框。

    **不看文本内容**——早期实现用「短文本 + 全角括号」识别标题，当调用方给的
    标题不含英文字母（如「教育背景」）时会把标题框误判成正文框，进而把正文写
    进标题框、原件正文残留 → 真实文字重叠（P2-4 agent 闭环暴露）。
    """
    for wsp in anchor.iter(WPS + "wsp"):
        if wsp.find(".//" + W + "txbxContent") is None:
            continue
        off_y, cy = _box_geom(wsp)
        if off_y <= 1.0 and cy >= TITLE_BOX_MIN_H_PT:
            return wsp
    return None


def find_body_wsp(anchor):
    """正文框（结构识别）：标题框之外、位于组合下方（off.y 最大）的有字文本框。

    回落顺序：① 有文字且 off.y 最大（正文在标题下方）→ ② 有文字且高度最大
    → ③ 无文字时取最高框（新栏目原型尚未填字）。
    """
    title = find_title_wsp(anchor)
    with_text: list[tuple[float, float, object]] = []
    empty: list[tuple[float, object]] = []
    for wsp in anchor.iter(WPS + "wsp"):
        if wsp is title:
            continue
        if wsp.find(".//" + W + "txbxContent") is None:
            continue
        off_y, cy = _box_geom(wsp)
        text = _box_text(wsp)
        if text.strip():
            with_text.append((off_y, cy, wsp))
        else:
            empty.append((cy, wsp))
    if with_text:
        with_text.sort(key=lambda t: (-t[0], -t[1]))
        return with_text[0][2]
    if empty:
        empty.sort(key=lambda t: -t[0])
        return empty[0][1]
    raise RuntimeError("栏目 anchor 内无正文框")


def strip_title_decorations(anchor) -> int:
    """删除克隆条目中的标题框 / 图标 / 横线（仅保留正文框）。

    用于「同栏目第 2+ 条」：复制条目不复制栏目标题。
    返回删除的对象数。wsp 识别：正文框=最高的有 txbxContent 大框；
    其余 wsp（标题框/Freeform 图标/Connector 横线）全删。
    """
    body = find_body_wsp(anchor)
    removed = 0
    # wgp 直接子节点与嵌套 grpSp 子节点中的 wsp 全部评估
    for wsp in list(anchor.iter(WPS + "wsp")):
        if wsp is body:
            continue
        parent = wsp.getparent()
        if parent is not None:
            parent.remove(wsp)
            removed += 1
    return removed
