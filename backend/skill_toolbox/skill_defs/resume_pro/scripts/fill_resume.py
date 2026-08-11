"""fill_resume.py — 简历模板字段填充（L1：文本替换 + 溢出预警）

用法:
  python fill_resume.py <template_dir> <data.json> <output.docx>

template_dir: skill 模板目录（含 template.docx + manifest.json）
data.json:    {"fields": {"name": "张三", "phone": "138-0000-8000", ...}}
              fields 的 key 为 manifest 中字段的 schema key（name/phone/email/
              address/birth/politics/intent/education/work/skill/summary/...）

替换规则（保留观感的最小侵入）:
  - line 字段（如 "姓    名：  余涵"）: 用新值替换锚点后的原文值部分，
    锚点文字（含冒号）与格式保留；支持同文本框多行字段各自替换。
  - block 字段（工作经历/教育/技能/自我评价）: 整个文本框文本替换为新内容，
    保留该文本框内首个 run 的 rPr（字体/字号/颜色）。
  - 多实例字段（work_0/work_1...）: 按 manifest 中 id 一一对应填入；
    数据缺项时该实例保留原模板文本。
  - 未标注的文本框（分区标题/装饰）原样不动。

溢出预警:
  替换后按 框宽(bbox) vs 字号估算 的文本行数 × 行高 对比框高，输出预警 JSON。
  中文按"1 字 ≈ 1 倍字号宽"，英文按 "0.55 × 字号宽"估算，行距按 1.3。
  仅估算，不保证精确——最终以渲染（render_pages）为准。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from zipfile import ZipFile

from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
WPG = "{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EMU_PER_PT = 12700
LINE_FACTOR = 1.3   # 行距系数（估算用）
LATIN_FACTOR = 0.55  # 英文字符平均宽度系数


def workspace_path(value: str, *, must_exist: bool = False) -> Path:
    root = Path.cwd().resolve()
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"路径越出任务工作区: {value}") from exc
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def template_dir(value: str) -> Path:
    root = (Path(__file__).resolve().parent.parent / "templates").resolve()
    path = Path(value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"模板目录不在内置模板库中: {value}") from exc
    return path


def emu2pt(v):
    try:
        return int(v) / EMU_PER_PT
    except (TypeError, ValueError):
        return None


# ---------------- 文本估算 ----------------
def estimate_lines(text: str, width_pt: float, font_pt: float) -> int:
    """按框宽估算文本占用的行数（含换行与自动换行）"""
    if not width_pt or not font_pt:
        return len(text.split("\n"))
    usable = width_pt * 0.94  # 内边距损耗
    total = 0
    for line in text.split("\n"):
        if not line:
            total += 1
            continue
        w = 0.0
        for ch in line:
            w += font_pt if ord(ch) > 0x2E80 else font_pt * LATIN_FACTOR
        total += max(1, int(w / usable) + (1 if w % usable else 0))
    return total


def estimate_height(text: str, width_pt: float, font_pt: float) -> float:
    if not font_pt:
        return len(text.split("\n")) * 20.0  # 未知字号：默认行高 20pt
    return estimate_lines(text, width_pt, font_pt) * font_pt * LINE_FACTOR


# ---------------- docx 解析 ----------------
def load_docx_xml(path: Path):
    with ZipFile(path) as z:
        return etree.fromstring(z.read("word/document.xml"))


def remove_photo_drawing(root, rId: str) -> bool:
    """按 rId 移除照片 drawing（用户未提供照片、选择留空时清掉模板示例头像）。

    r:embed 是 <a:blip> 上的属性（不是元素），所以要对所有元素查 .get(R+"embed")，
    命中后向上找最近的 drawing 祖先整块移除。
    """
    for el in root.iter():
        if el.get(R + "embed") != rId:
            continue
        node = el
        drawing = None
        while node is not None:
            if node.tag.endswith("drawing"):
                drawing = node
                break
            node = node.getparent()
        if drawing is not None and drawing.getparent() is not None:
            drawing.getparent().remove(drawing)
            return True
    return False


def save_docx_xml(
    root,
    src_path: Path,
    out_path: Path,
    photos: list | None = None,
    skip_media: set | None = None,
) -> None:
    """写回: 复制原 docx 全部内容，仅替换 word/document.xml（避免 zip 内重复条目）。

    photos: [{rId, old_media, user_image: Path}] —— 替换照片位：
      跳过旧 media 条目，写入用户图片（新文件名保留用户扩展名），
      并把 document.xml.rels 中对应 rId 的 Target 指向新文件。
    skip_media: 需要丢弃的旧 media 成员（如照片被 __remove__ 移除）。
    """
    import re
    import zipfile
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    photos = photos or []

    def _new_media_name(old_media: str, user_image: Path) -> str:
        ext = user_image.suffix.lower() or ".jpg"
        return f"word/media/{Path(old_media).stem}_user{ext}"

    def _rels_target(old_media: str, user_image: Path) -> str:
        # rels 文件在 word/_rels/ 下，Target 相对 word/ 目录
        ext = user_image.suffix.lower() or ".jpg"
        return f"media/{Path(old_media).stem}_user{ext}"

    with ZipFile(src_path) as zin, zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            name = item.filename
            if name == "word/document.xml":
                zout.writestr(item, xml)
                continue
            # 照片处理
            photo = next((p for p in photos if p["old_media"] == name), None)
            if photo is not None:
                continue  # 旧照片字节不复制
            if skip_media and name in skip_media:
                continue  # 照片被 __remove__ 移除，丢弃旧媒体
            if name == "word/_rels/document.xml.rels" and photos:
                # 用 XML 解析改写 Target，避免正则对属性顺序（Id 在 Target 前/后）敏感
                # 导致照片位指向已删除的旧媒体（静默 broken image）。
                rels_root = etree.fromstring(zin.read(name))
                for p in photos:
                    for rel in rels_root:
                        if rel.get("Id") == p["rId"]:
                            rel.set("Target", _rels_target(p["old_media"], p["user_image"]))
                            break
                zout.writestr(
                    item,
                    etree.tostring(rels_root, xml_declaration=True, encoding="UTF-8", standalone=True),
                )
                continue
            if name == "[Content_Types].xml" and photos:
                # 为用户图片扩展名补 Default 条目（缺则 Word 报文件损坏）
                ct = zin.read(name).decode("utf-8")
                for p in photos:
                    ext = p["user_image"].suffix.lstrip(".") or "jpg"
                    if not re.search(rf'<Default Extension="{re.escape(ext)}"', ct, re.IGNORECASE):
                        ct = ct.replace(
                            "</Types>",
                            f'<Default Extension="{ext}" ContentType="image/{ext}"/></Types>',
                        )
                zout.writestr(item, ct)
                continue
            zout.writestr(item, zin.read(name))
        # 写入用户照片文件
        for p in photos:
            new_name = _new_media_name(p["old_media"], p["user_image"])
            zout.writestr(new_name, p["user_image"].read_bytes())


def iter_box_texts(root):
    """yield (anchor_idx, wsp_idx, tx, text, width_pt, height_pt, font_pt)"""
    for a_idx, a in enumerate(root.findall(".//" + WP + "anchor")):
        ext = a.find(WP + "extent")
        a_w = emu2pt(ext.get("cx")) if ext is not None else None
        a_h = emu2pt(ext.get("cy")) if ext is not None else None
        for wsp_idx, wsp in enumerate(a.iter(WPS + "wsp")):
            tx = wsp.find(".//" + W + "txbxContent")
            if tx is None:
                continue
            paras = []
            for p in tx.iter(W + "p"):
                t = "".join(x.text or "" for x in p.iter(W + "t"))
                paras.append(t)
            text = "\n".join(paras)
            w_pt, h_pt = _box_bbox(wsp, a_w, a_h)
            sz = _box_font_size(tx)
            yield a_idx, wsp_idx, tx, text, w_pt, h_pt, sz


def _box_bbox(wsp, a_w, a_h):
    sp = wsp.find(WPS + "spPr")
    if sp is None:
        return None, None
    xfrm = sp.find(A + "xfrm")
    if xfrm is None:
        return None, None
    ext = xfrm.find(A + "ext")
    if ext is None or not ext.get("cx") or not ext.get("cy"):
        return None, None
    sx = sy = 1.0
    grp = wsp.getparent()
    if grp is not None and grp.tag == WPG + "grpSp":
        gpr = grp.find(WPG + "grpSpPr")
        if gpr is not None:
            gxf = gpr.find(A + "xfrm")
            if gxf is not None:
                chExt = gxf.find(A + "chExt")
                if chExt is not None:
                    if a_w and chExt.get("cx") and float(chExt.get("cx")) > 0:
                        sx = a_w * EMU_PER_PT / float(chExt.get("cx"))
                    if a_h and chExt.get("cy") and float(chExt.get("cy")) > 0:
                        sy = a_h * EMU_PER_PT / float(chExt.get("cy"))
    return (int(ext.get("cx")) / EMU_PER_PT * sx), (int(ext.get("cy")) / EMU_PER_PT * sy)


def _box_font_size(tx):
    for r in tx.iter(W + "r"):
        rpr = r.find(W + "rPr")
        if rpr is not None:
            sz = rpr.find(W + "sz")
            if sz is not None and sz.get(W + "val"):
                return int(sz.get(W + "val")) / 2
    return None


def box_text_exact(tx) -> str:
    """文本框当前精确文本（含段间换行）"""
    paras = []
    for p in tx.iter(W + "p"):
        paras.append("".join(t.text or "" for t in p.iter(W + "t")))
    return "\n".join(paras)


# ---------------- 替换 ----------------
def replace_text_in_paragraphs(tx, old_text: str, new_text: str, keep_anchor: bool = True) -> int:
    """在文本框内按文本替换。

    keep_anchor=True（line 字段）: 只替换锚点（含冒号）之后的值部分。
    keep_anchor=False（block 字段）: 整个文本框文本替换。
    返回替换次数。
    """
    paras = list(tx.iter(W + "p"))
    if keep_anchor:
        return _replace_line_value(tx, paras, old_text, new_text)
    return _replace_block(tx, paras, new_text)


def _replace_block(tx, paras, new_text: str) -> int:
    """整块替换: 保留第一段第一个 run 的 rPr，清空其余内容"""
    runs_all = list(tx.iter(W + "r"))
    if not runs_all:
        return 0
    first_run = runs_all[0]
    rpr = first_run.find(W + "rPr")

    # 找第一段
    first_p = paras[0]
    # 删掉除第一段外的所有段落
    for p in paras[1:]:
        tx.remove(p)
    # 第一段内：保留第一个 run 的 rPr，删其余 run
    for r in list(first_p.iter(W + "r")):
        if r is not first_run:
            first_p.remove(r)
    # 第一段内可能还有书签/域等，保留；run 文本设为新内容（拆成多段）
    if rpr is None:
        rpr = etree.SubElement(first_run, W + "rPr")
    # 删掉 first_run 现有的所有 w:t
    for t in first_run.findall(W + "t"):
        first_run.remove(t)

    lines = new_text.split("\n")
    # 若有多行，给第一行所在的 run 设置文本后补段落
    # 先处理第一行
    _set_run_text(first_run, rpr, lines[0] if lines else "")
    # 其余行：在 first_p 后插入新段落（复制 rPr）
    for extra in lines[1:]:
        new_p = etree.Element(W + "p")
        # 复制第一段的 pPr（如果有）保持格式
        ppr = first_p.find(W + "pPr")
        if ppr is not None:
            new_p.append(etree.fromstring(etree.tostring(ppr)))
        new_r = etree.SubElement(new_p, W + "r")
        new_r.append(etree.fromstring(etree.tostring(rpr)))
        _set_run_text(new_r, rpr, extra)
        first_p.addnext(new_p)
        first_p = new_p
    return 1


def _set_run_text(run, rpr, text: str) -> None:
    t = etree.SubElement(run, W + "t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text


def _replace_line_value(tx, paras, old_line: str, new_text: str) -> int:
    """line 字段: 旧行匹配锚点行，替换锚点后的值部分。

    old_line 形如 "姓    名：  余涵"。锚点 = 冒号前（含冒号）部分。
    新值 = 锚点 + 新文本。锚点文字、空格格式保留。
    匹配对空白不敏感（模板常含多个连续空格对齐）。
    若新值自身已带锚点标签（如"手机：138-…"），去重后不再重复拼接。
    """
    m = re.match(r"^([^：:]*[：:])", old_line)
    anchor = m.group(1) if m else ""
    # 新值已含锚点 → 直接用新值；否则锚点 + 新值。
    # 模板锚点常含多个对齐空格（如"电    话："），归一化空白后再比对，
    # 否则模型违规带标签填值"电话：138-…"时会被拼成"电    话：电话：138-…"。
    anchor_norm = re.sub(r"\s+", "", anchor)
    new_norm = re.sub(r"\s+", "", new_text)
    if anchor_norm and new_norm.startswith(anchor_norm):
        new_full = new_text
    else:
        new_full = anchor + new_text if anchor else new_text
    old_norm = re.sub(r"\s+", "", old_line)
    replaced = 0
    for p in paras:
        ptext = "".join(t.text or "" for t in p.iter(W + "t"))
        if old_norm and old_norm in re.sub(r"\s+", "", ptext):
            _set_paragraph_text(p, new_full)
            replaced += 1
    return replaced


def _set_paragraph_text(p, new_text: str) -> None:
    """整段替换: 保留第一个 run 的 rPr，删其余 run"""
    runs = list(p.iter(W + "r"))
    if not runs:
        return
    first = runs[0]
    rpr = first.find(W + "rPr")
    for r in runs[1:]:
        p.remove(r)
    for t in first.findall(W + "t"):
        first.remove(t)
    t = etree.SubElement(first, W + "t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = new_text
    # 清除断字/空白差异
    if rpr is not None and rpr.find(W + "noProof") is None:
        pass  # 保留 rPr 原样


# ---------------- 受限组件动作（阶段 5） ----------------
#
# 只允许 manifest components[].allowed_actions 列出的动作；动作执行到
# workspace 候选副本，任一步失败则丢弃候选，不在半修改文件上继续。
# 无 Vision 模式只执行白名单动作与机械门（实施计划 §11.2/§11.3）。

ALLOWED_ACTIONS = {"replace_text", "replace_asset", "resize_component", "shift_components", "clone_component"}
PAGE_HEIGHT_PT = 794.0  # A4 页高（11.69in×72）


def verify_template_hash(tpl_dir: Path, manifest: dict) -> None:
    """template_sha256 不匹配立即失败并提示重新索引，禁止对未知结构套旧 selector。"""
    expected = manifest.get("template_sha256")
    if not expected:
        return  # 旧 manifest（未索引）不阻断 L1 原位替换
    template = tpl_dir / manifest.get("template_file", "template.docx")
    if not template.is_file():
        raise RuntimeError(f"模板文件不存在: {template}")
    import hashlib

    actual = hashlib.sha256(template.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"模板 hash 不匹配（期望 {expected[:16]}…，实际 {actual[:16]}…）。"
            "模板文件已被修改，请重新索引后再操作，禁止对未知结构继续套用旧 selector。"
        )


def _wsp_by_loc(root, loc: dict):
    """按 (anchor_idx, wsp_idx) 定位文本框 lxml 元素；找不到返回 None。"""
    anchors = root.findall(".//" + WP + "anchor")
    idx = int(loc.get("anchor_idx", -1))
    if idx < 0 or idx >= len(anchors):
        return None
    wsps = list(anchors[idx].iter(WPS + "wsp"))
    widx = int(loc.get("wsp_idx", -1))
    if widx < 0 or widx >= len(wsps):
        return None
    return wsps[widx]


def _ancestor(element, tag: str):
    node = element
    while node is not None:
        if node.tag == tag:
            return node
        node = node.getparent()
    return None


def _shift_anchor(anchor, delta_emu: int) -> None:
    position = anchor.find(WP + "positionV/" + WP + "posOffset")
    if position is not None and position.text:
        position.text = str(int(position.text) + delta_emu)
    for wsp in anchor.iter(WPS + "wsp"):
        off = wsp.find(WPS + "spPr/" + A + "xfrm/" + A + "off")
        if off is not None and off.get("y") is not None:
            off.set("y", str(int(off.get("y")) + delta_emu))


def _anchor_bounds(anchor) -> tuple[int, int]:
    position = anchor.find(WP + "positionV/" + WP + "posOffset")
    extent = anchor.find(WP + "extent")
    if (
        position is not None
        and position.text
        and extent is not None
        and extent.get("cy")
    ):
        top = int(position.text)
        return top, top + int(extent.get("cy"))
    ys: list[int] = []
    bottoms: list[int] = []
    for wsp in anchor.iter(WPS + "wsp"):
        xfrm = wsp.find(WPS + "spPr/" + A + "xfrm")
        off = xfrm.find(A + "off") if xfrm is not None else None
        ext = xfrm.find(A + "ext") if xfrm is not None else None
        if off is None or ext is None or off.get("y") is None or ext.get("cy") is None:
            continue
        y = int(off.get("y"))
        ys.append(y)
        bottoms.append(y + int(ext.get("cy")))
    if not ys:
        raise RuntimeError("组件 anchor 缺少可定位的 xfrm")
    return min(ys), max(bottoms)


def _anchor_rect(anchor) -> tuple[int, int, int, int] | None:
    x = anchor.find(WP + "positionH/" + WP + "posOffset")
    y = anchor.find(WP + "positionV/" + WP + "posOffset")
    extent = anchor.find(WP + "extent")
    if (
        x is None
        or not x.text
        or y is None
        or not y.text
        or extent is None
        or not extent.get("cx")
        or not extent.get("cy")
    ):
        return None
    left, top = int(x.text), int(y.text)
    return left, top, left + int(extent.get("cx")), top + int(extent.get("cy"))


def _component_anchors(root, manifest: dict) -> list:
    all_anchors = root.findall(".//" + WP + "anchor")
    anchors: list = []
    seen: set[int] = set()
    for comp in manifest.get("components", []):
        for obj in comp.get("objects", []):
            index = int(obj.get("anchor_idx", -1))
            if 0 <= index < len(all_anchors) and index not in seen:
                seen.add(index)
                anchors.append(all_anchors[index])
    return anchors


def _overlap_ratios(anchors: list) -> dict[tuple[str, str], float]:
    entries: list[tuple[str, tuple[int, int, int, int]]] = []
    for index, anchor in enumerate(anchors):
        rect = _anchor_rect(anchor)
        if rect is None:
            continue
        ids = sorted(
            node.get("id")
            for node in anchor.iter(WP + "docPr")
            if node.get("id")
        )
        key = "docPr:" + ",".join(ids) if ids else f"anchor:{index}"
        entries.append((key, rect))
    overlaps: dict[tuple[str, str], float] = {}
    for index, (left_key, left) in enumerate(entries):
        for right_key, right in entries[index + 1 :]:
            width = max(0, min(left[2], right[2]) - max(left[0], right[0]))
            height = max(0, min(left[3], right[3]) - max(left[1], right[1]))
            if not width or not height:
                continue
            left_area = max(1, (left[2] - left[0]) * (left[3] - left[1]))
            right_area = max(1, (right[2] - right[0]) * (right[3] - right[1]))
            overlaps[tuple(sorted((left_key, right_key)))] = (
                width * height / min(left_area, right_area)
            )
    return overlaps


def _reject_new_overlaps(before: dict[tuple[str, str], float], anchors: list) -> None:
    after = _overlap_ratios(anchors)
    added = [
        pair
        for pair, ratio in after.items()
        if ratio >= 0.20 and ratio > before.get(pair, 0.0) + 0.05
    ]
    if added:
        raise RuntimeError("组件动作会新增严重重叠: " + ", ".join("/".join(p) for p in added))


def _expanded_shift_ids(component_ids: set[str], components: dict[str, dict]) -> set[str]:
    expanded = set(component_ids)
    pending = list(component_ids)
    while pending:
        comp_id = pending.pop()
        comp = components.get(comp_id)
        if comp is None:
            continue
        for linked in comp.get("moves_with", []):
            if linked not in expanded:
                expanded.add(linked)
                pending.append(linked)
    return expanded


def _shared_anchor_components(manifest: dict, selected: set[str]) -> dict[int, set[str]]:
    groups: dict[int, set[str]] = {}
    for component in manifest.get("components", []):
        component_id = component["id"]
        for obj in component.get("objects", []):
            index = int(obj.get("anchor_idx", -1))
            if index >= 0:
                groups.setdefault(index, set()).add(component_id)
    return {
        index: component_ids - selected
        for index, component_ids in groups.items()
        if component_ids & selected and component_ids - selected
    }


def _shift_ys(root, dy_pt: float, component_ids: set[str], manifest: dict) -> None:
    """纵向平移指定组件（含其 objects 与 moves_with 列出的其它组件）。

    只允许 manifest 明确列出的组件移动；dy_pt 为正向下、负向上。
    """
    all_anchors = root.findall(".//" + WP + "anchor")
    anchors_to_shift: list = []
    seen: set[int] = set()
    for comp in manifest.get("components", []):
        cid = comp["id"]
        if cid not in component_ids:
            continue
        for obj in comp.get("objects", []):
            index = int(obj.get("anchor_idx", -1))
            if 0 <= index < len(all_anchors) and index not in seen:
                seen.add(index)
                anchors_to_shift.append(all_anchors[index])
    if not anchors_to_shift:
        raise RuntimeError("shift_components 未找到目标 anchor")
    delta = round(dy_pt * EMU_PER_PT)
    page_height = round(PAGE_HEIGHT_PT * EMU_PER_PT)
    for anchor in anchors_to_shift:
        top, bottom = _anchor_bounds(anchor)
        page_top = max(0, top // page_height) * page_height
        if top + delta < page_top or bottom + delta > page_top + page_height:
            raise RuntimeError("shift_components 会使组件越出页面边界")
        _shift_anchor(anchor, delta)


def _resize_component(root, comp: dict, height_pt: float) -> None:
    """只改变 manifest 标注的可拉伸对象高度；background 与文字框若同组件则同步。

    height_pt 受 comp.min_height_pt/max_height_pt 约束（机械门）。
    """
    lo = comp.get("min_height_pt") or 0.0
    hi = comp.get("max_height_pt") or PAGE_HEIGHT_PT
    if height_pt < lo or height_pt > hi:
        raise RuntimeError(
            f"resize_component({comp['id']}) 高度 {height_pt}pt 超出允许范围 "
            f"[{lo}, {hi}]pt"
        )
    for obj in comp.get("objects", []):
        wsp = _wsp_by_loc(root, obj)
        if wsp is None:
            continue
        xfrm = wsp.find(WPS + "spPr/" + A + "xfrm")
        ext = xfrm.find(A + "ext") if xfrm is not None else None
        if ext is None or ext.get("cy") is None:
            continue
        ext.set("cy", str(round(height_pt * EMU_PER_PT)))
        anchor = _ancestor(wsp, WP + "anchor")
        anchor_ext = anchor.find(WP + "extent") if anchor is not None else None
        if anchor_ext is not None:
            anchor_ext.set("cy", str(round(height_pt * EMU_PER_PT)))


def _clone_component(
    root, src_comp: dict, after_comp: dict, value: str | None = None
) -> tuple[dict, list]:
    """Clone complete drawings, assign unique IDs, and place them after a component."""
    import copy

    if not src_comp.get("objects"):
        raise RuntimeError(f"clone_component({src_comp['id']}) 无对象可复制")
    src_wsps = [_wsp_by_loc(root, obj) for obj in src_comp["objects"]]
    if any(wsp is None for wsp in src_wsps):
        raise RuntimeError(f"clone_component({src_comp['id']}) 源组件定位失败")
    after_wsps = [_wsp_by_loc(root, obj) for obj in after_comp.get("objects", [])]
    if not after_wsps or any(wsp is None for wsp in after_wsps):
        raise RuntimeError(f"clone_component({after_comp['id']}) 目标位置定位失败")

    source_anchors = []
    for wsp in src_wsps:
        anchor = _ancestor(wsp, WP + "anchor")
        if anchor is not None and anchor not in source_anchors:
            source_anchors.append(anchor)
    after_anchors = [_ancestor(wsp, WP + "anchor") for wsp in after_wsps]
    after_anchors = [anchor for anchor in after_anchors if anchor is not None]
    if not source_anchors or not after_anchors:
        raise RuntimeError("clone_component 缺少完整 wp:anchor")

    source_top = min(_anchor_bounds(anchor)[0] for anchor in source_anchors)
    target_top = max(_anchor_bounds(anchor)[1] for anchor in after_anchors) + round(
        6 * EMU_PER_PT
    )
    delta = target_top - source_top
    clone_bottom = max(_anchor_bounds(anchor)[1] + delta for anchor in source_anchors)
    page_height = round(PAGE_HEIGHT_PT * EMU_PER_PT)
    page_top = max(0, target_top // page_height) * page_height
    if clone_bottom > page_top + page_height:
        raise RuntimeError("clone_component 会使副本越出页面边界")

    doc_pr_ids = [
        int(node.get("id"))
        for node in root.iter(WP + "docPr")
        if (node.get("id") or "").isdigit()
    ]
    next_id = max(doc_pr_ids, default=0) + 1
    relative_heights = [
        int(anchor.get("relativeHeight"))
        for anchor in root.iter(WP + "anchor")
        if (anchor.get("relativeHeight") or "").isdigit()
    ]
    next_height = max(relative_heights, default=0) + 1
    insertion = _ancestor(after_anchors[-1], W + "drawing")
    if insertion is None:
        raise RuntimeError("clone_component 目标 drawing 定位失败")

    inserted_wsps: list = []
    inserted_anchors: list = []
    for source_anchor in source_anchors:
        source_drawing = _ancestor(source_anchor, W + "drawing")
        if source_drawing is None:
            raise RuntimeError("clone_component 源 drawing 定位失败")
        new_drawing = copy.deepcopy(source_drawing)
        new_anchor = new_drawing.find(".//" + WP + "anchor")
        if new_anchor is None:
            raise RuntimeError("clone_component 副本 anchor 丢失")
        _shift_anchor(new_anchor, delta)
        new_anchor.set("relativeHeight", str(next_height))
        next_height += 1
        for doc_pr in new_anchor.iter(WP + "docPr"):
            doc_pr.set("id", str(next_id))
            doc_pr.set("name", f"{doc_pr.get('name', 'Component')} Copy {next_id}")
            next_id += 1
        for node in new_anchor.iter():
            for attr_name in list(node.attrib):
                if etree.QName(attr_name).localname in {"anchorId", "editId"}:
                    node.set(attr_name, f"{next_id:08X}")
                    next_id += 1
        insertion.addnext(new_drawing)
        insertion = new_drawing
        inserted_anchors.append(new_anchor)
        inserted_wsps.extend(new_anchor.iter(WPS + "wsp"))
    if not inserted_wsps:
        raise RuntimeError(f"clone_component({src_comp['id']}) 未插入任何对象")
    if value is not None:
        target = next(
            (
                wsp.find(".//" + W + "txbxContent")
                for wsp in inserted_wsps
                if wsp.find(".//" + W + "txbxContent") is not None
            ),
            None,
        )
        if target is None:
            raise RuntimeError("clone_component 副本没有可填充文本槽")
        replace_text_in_paragraphs(target, box_text_exact(target), value, keep_anchor=False)
    return (
        {
            "clone_id": f"{src_comp['id']}_clone_{next_id}",
            "source": src_comp["id"],
            "after": after_comp["id"],
            "dy_pt": round(delta / EMU_PER_PT, 2),
        },
        inserted_anchors,
    )


def apply_component_actions(root, manifest: dict, values: dict) -> tuple[list, list[str]]:
    """执行数据里的受限动作，返回 (记录列表, 警告列表)。

    values 支持两个来源（保持兼容）：
      - 旧 `fields`（原位替换，L1 语义不变）
      - 新 `actions`: [{action, component, value|height_pt|component_ids|after, dy_pt}]
    动作必须在组件 allowed_actions 白名单内。
    """
    records: list[dict] = []
    warnings: list[str] = []
    components = {c["id"]: c for c in manifest.get("components", [])}
    actions = list(values.get("actions", []))
    actions.sort(key=lambda item: item.get("action") == "clone_component")
    for action_spec in actions:
        action = action_spec.get("action")
        if action not in ALLOWED_ACTIONS:
            warnings.append(f"未知动作: {action}（允许 {sorted(ALLOWED_ACTIONS)}）")
            continue
        comp_id = action_spec.get("component")
        # shift_components 按 component_ids 批量移动，component 可选
        if action == "shift_components":
            ids = set(action_spec.get("component_ids", []))
            dy_pt = float(action_spec.get("dy_pt", 0))
            unknown = sorted(i for i in ids if i not in components)
            if unknown:
                warnings.append(f"shift_components 引用未知组件: {unknown}")
                continue
            if not ids:
                warnings.append("shift_components 缺少 component_ids")
                continue
            ids = _expanded_shift_ids(ids, components)
            unknown = sorted(i for i in ids if i not in components)
            if unknown:
                warnings.append(f"shift_components moves_with 引用未知组件: {unknown}")
                continue
            forbidden = sorted(
                comp_id
                for comp_id in ids
                if action not in components[comp_id].get("allowed_actions", [])
            )
            if forbidden:
                warnings.append(f"组件 {forbidden} 不允许动作 shift_components")
                continue
            shared = _shared_anchor_components(manifest, ids)
            if shared:
                details = ", ".join(
                    f"anchor {index} 还包含 {sorted(missing)}"
                    for index, missing in sorted(shared.items())
                )
                warnings.append(
                    "shift_components 必须同时选择共享 anchor 的全部组件: " + details
                )
                continue
            anchors = _component_anchors(root, manifest)
            before = _overlap_ratios(anchors)
            _shift_ys(root, dy_pt, ids, manifest)
            _reject_new_overlaps(before, anchors)
            records.append({"action": action, "component_ids": sorted(ids), "dy_pt": dy_pt})
            continue
        if not comp_id or comp_id not in components:
            warnings.append(f"动作 {action} 引用了未知组件: {comp_id}")
            continue
        comp = components.get(comp_id)
        allowed = comp.get("allowed_actions", []) if comp else []
        if action not in allowed:
            warnings.append(f"组件 {comp_id} 不允许动作 {action}（白名单 {allowed}）")
            continue
        if action == "replace_text":
            value = str(action_spec.get("value", ""))
            field_id = action_spec.get("field")
            # 缺省用组件 content_slot 的首个字段
            if not field_id:
                for slot_field in comp.get("content_slot", []):
                    if any(f["id"] == slot_field for f in manifest["fields"]):
                        field_id = slot_field
                        break
            if not field_id:
                warnings.append(f"replace_text({comp_id}) 无法确定目标字段")
                continue
            # 复用 L1 文本替换：按 component field 的 location 定位文本框。
            # line 字段按 manifest 模板行（f["text"]）匹配锚点行；block 字段
            # 整块替换（传全文）。
            replaced_any = False
            for f in manifest["fields"]:
                if f["id"] != field_id:
                    continue
                keep_anchor = f.get("mode", "line") != "block"
                for loc in f.get("locations", []):
                    wsp = _wsp_by_loc(root, loc)
                    if wsp is None:
                        continue
                    tx = wsp.find(".//" + W + "txbxContent")
                    if tx is None:
                        continue
                    if keep_anchor:
                        tpl_line = f.get("text") or ""
                        n = replace_text_in_paragraphs(tx, tpl_line, value, keep_anchor=True)
                    else:
                        n = replace_text_in_paragraphs(tx, box_text_exact(tx), value, keep_anchor=False)
                    if n:
                        replaced_any = True
                        records.append({"action": action, "component": comp_id, "field": field_id, "count": n})
                        break
                if replaced_any:
                    break
            if not replaced_any:
                warnings.append(f"replace_text({comp_id}/{field_id}) 未找到匹配文本框")
            continue
        if action == "replace_asset":
            value = str(action_spec.get("value", ""))
            if value == "__remove__":
                removed_any = False
                for f in manifest["fields"]:
                    if f.get("mode") != "photo":
                        continue
                    for loc in f.get("locations", []):
                        r_id = loc.get("rId", "")
                        media = loc.get("media", "")
                        # 同步执行 L1 的照片移除：去掉 drawing + 丢弃旧媒体字节。
                        # 这里把 media 路径通过 values.photo 的隐藏键回传主流程，
                        # 让 save_docx_xml 的 skip_media 生效（动作层只改 root，
                        # 主流程的 photos/skip_media 集合在动作执行后才保存）。
                        if remove_photo_drawing(root, r_id) and media:
                            action_spec["_skip_media"] = media
                            removed_any = True
                            records.append({"action": action, "component": comp_id, "value": "__remove__"})
                            break
                if not removed_any:
                    warnings.append(f"replace_asset({comp_id}) 未找到照片位")
            else:
                photo_ids = {
                    field["id"]
                    for field in manifest["fields"]
                    if field.get("mode") == "photo"
                }
                if not photo_ids.intersection(comp.get("field_ids", [])):
                    warnings.append(f"replace_asset({comp_id}) 没有照片内容槽")
                    continue
                image = workspace_path(value)
                if values.get("photo") == value and image.is_file():
                    records.append(
                        {"action": action, "component": comp_id, "value": value}
                    )
                else:
                    warnings.append(f"replace_asset({comp_id}) 图片不存在: {value}")
            continue
        if action == "resize_component":
            height_pt = float(action_spec.get("height_pt", 0))
            anchors = _component_anchors(root, manifest)
            before = _overlap_ratios(anchors)
            _resize_component(root, comp, height_pt)
            _reject_new_overlaps(before, anchors)
            records.append({"action": action, "component": comp_id, "height_pt": height_pt})
            continue
        if action == "clone_component":
            after_id = action_spec.get("after")
            if not after_id or after_id not in components:
                warnings.append(f"clone_component 缺少合法 after: {after_id}")
                continue
            anchors = _component_anchors(root, manifest)
            before = _overlap_ratios(anchors)
            clone, inserted_anchors = _clone_component(
                root,
                comp,
                components[after_id],
                str(action_spec["value"]) if "value" in action_spec else None,
            )
            _reject_new_overlaps(before, [*anchors, *inserted_anchors])
            records.append({"action": action, "component": comp_id, **clone})
            continue
    return records, warnings


# ---------------- 主流程 ----------------
def main() -> None:
    if len(sys.argv) != 4:
        print("用法: python fill_resume.py <template_dir> <data.json> <output.docx>", file=sys.stderr)
        sys.exit(2)
    tpl_dir = template_dir(sys.argv[1])
    data_path = workspace_path(sys.argv[2])
    out_path = workspace_path(sys.argv[3])
    if out_path.suffix.lower() != ".docx":
        raise RuntimeError("output 必须是工作区内的 .docx 路径")
    out_path.parent.mkdir(parents=True, exist_ok=True)  # 产物目录可能不存在（artifacts/）

    template = tpl_dir / "template.docx"
    manifest_path = tpl_dir / "manifest.json"
    if not template.is_file() or not manifest_path.is_file():
        print(f"[错误] 模板目录缺 template.docx/manifest.json: {tpl_dir}", file=sys.stderr)
        sys.exit(1)
    if not data_path.is_file():
        print(
            f"[错误] 数据文件不存在: {data_path}（请先用 write 写 work/resume_data.json）",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"[错误] 数据文件不是合法 JSON: {data_path} — {exc}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    values = dict(data.get("fields", {}))
    # 顶层 actions 数组也作为动作来源（与 fields 内嵌 actions 兼容）
    values.setdefault("actions", data.get("actions", []))
    # 阶段 5：模板 hash 校验（不匹配直接失败，禁止对未知结构套旧 selector）
    verify_template_hash(tpl_dir, manifest)

    # 非删除型 replace_asset 复用既有 photo 字段写入链路；动作层只负责
    # 白名单与记录，不复制 relationship/media 实现。
    components = {item["id"]: item for item in manifest.get("components", [])}
    photo_ids = {
        field["id"] for field in manifest["fields"] if field.get("mode") == "photo"
    }
    for action_spec in values.get("actions", []):
        comp = components.get(action_spec.get("component"))
        value = str(action_spec.get("value", ""))
        if (
            action_spec.get("action") == "replace_asset"
            and value != "__remove__"
            and comp is not None
            and "replace_asset" in comp.get("allowed_actions", [])
            and photo_ids.intersection(comp.get("field_ids", []))
        ):
            values["photo"] = value

    root = load_docx_xml(template)
    # 构建 manifest 字段索引: (anchor_idx, wsp_idx) → [fields...]
    # 同一文本框可含多个 line 字段（如"姓 名：X 年 龄：Y"），按行分别匹配
    loc2fields: dict[tuple, list] = {}
    photo_fields: list[dict] = []
    for f in manifest["fields"]:
        if f.get("mode") == "photo":
            photo_fields.append(f)
            continue
        for loc in f["locations"]:
            loc2fields.setdefault((loc["anchor_idx"], loc["wsp_idx"]), []).append(f)

    replaced: list[dict] = []
    overflow: list[dict] = []
    warnings: list[str] = []
    photos: list[dict] = []
    skip_media: set[str] = set()

    # 照片处理：values["photo"] = 用户图片路径；特殊值 "__remove__" 表示移除照片位。
    for pf in photo_fields:
        val = values.get("photo") or values.get(pf["id"]) or values.get(pf["key"])
        if not val or not str(val).strip():
            continue
        loc = pf["locations"][0]
        rId = loc.get("rId", "rId4")
        media = loc.get("media", "word/media/image1.jpeg")
        if str(val).strip() == "__remove__":
            # 用户选择不要照片：移除模板照片 drawing + 丢弃旧媒体字节
            if remove_photo_drawing(root, rId):
                skip_media.add(media)
                replaced.append({"id": "photo", "key": "photo", "count": 1, "new_text": "（已移除示例照片）"})
            else:
                warnings.append("photo: 未找到照片位 drawing，未能移除")
            continue
        user_img = workspace_path(str(val))
        if not user_img.is_file():
            warnings.append(f"photo: 图片不存在 {user_img}，保留模板原照片")
            continue
        photos.append({
            "rId": rId,
            "old_media": media,
            "user_image": user_img,
        })
        replaced.append({"id": "photo", "key": "photo", "count": 1, "new_text": f"照片 {user_img.name}"})

    # 收集所有文本框（id 定位）
    boxes = list(iter_box_texts(root))
    for a_idx, wsp_idx, tx, text, w_pt, h_pt, font_pt in boxes:
        field_list = loc2fields.get((a_idx, wsp_idx))
        if not field_list:
            continue
        # 该文本框所有字段逐一处理（line 按各自 text 行匹配）
        for f in field_list:
            field_id = f["id"]
            key = f["key"]
            # 数据缺项 → 保留模板原文
            if field_id not in values and key not in values:
                continue
            new_value = values.get(field_id, values.get(key, ""))
            if new_value is None or not new_value.strip():
                continue

            mode = f.get("mode", "line")
            try:
                if mode == "block":
                    n = replace_text_in_paragraphs(tx, box_text_exact(tx), new_value, keep_anchor=False)
                else:
                    # line: 匹配 manifest text（模板原文行）所在段落
                    tpl_line = f["text"]
                    n = replace_text_in_paragraphs(tx, tpl_line, new_value, keep_anchor=True)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{field_id}: 替换异常 {e}")
                continue
            if n == 0:
                warnings.append(f"{field_id}: 未找到匹配文本，跳过（模板 {tpl_dir.name} 结构可能已变）")
                continue
            replaced.append({"id": field_id, "key": key, "count": n, "new_text": new_value[:40]})

            # 溢出估算
            est_h = estimate_height(new_value, w_pt or 0, font_pt or 10)
            if h_pt and est_h > h_pt * 1.05:
                overflow.append({
                    "id": field_id, "key": key,
                    "box_pt": [round(w_pt, 1) if w_pt else None, round(h_pt, 1) if h_pt else None],
                    "font_pt": font_pt,
                    "estimated_lines": max(1, int(est_h / (font_pt * LINE_FACTOR)) if font_pt else len(new_value.split("\n"))),
                    "estimated_height_pt": round(est_h, 1),
                    "text": new_value[:60],
                    "hint": "内容可能超出文本框，需精简或换行适配（L2 版式重排二期支持自动扩框）",
                })

    # 阶段 5：受限组件动作（replace_text/replace_asset/resize/shift/clone）。
    # 动作失败（白名单外/定位失败/hash 不匹配）已在上游 fail-fast；这里把
    # 动作记录并入 replaced，供交付说明与 QA 状态使用。
    action_records, action_warnings = apply_component_actions(root, manifest, values)
    replaced.extend(action_records)
    warnings.extend(action_warnings)
    # replace_asset __remove__ 通过动作 spec 的 _skip_media 回传媒体路径，
    # 让 save_docx_xml 丢弃旧照片字节（动作层只改 root，不能直接改 zip）。
    for action_spec in values.get("actions", []):
        media = action_spec.get("_skip_media")
        if media:
            skip_media.add(media)

    save_docx_xml(root, template, out_path, photos=photos, skip_media=skip_media)

    print(json.dumps({
        "ok": True,
        "output": str(out_path),
        "template": manifest["id"],
        "replaced": replaced,
        "overflow_risks": overflow,
        "warnings": warnings,
        "actions": action_records,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
