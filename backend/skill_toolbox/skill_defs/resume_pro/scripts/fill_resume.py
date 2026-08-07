# -*- coding: utf-8 -*-
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
import shutil
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
    import zipfile
    import re
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


# ---------------- 主流程 ----------------
def main() -> None:
    if len(sys.argv) != 4:
        print("用法: python fill_resume.py <template_dir> <data.json> <output.docx>", file=sys.stderr)
        sys.exit(2)
    tpl_dir = Path(sys.argv[1])
    data_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3])
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
    values = data.get("fields", {})

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
        user_img = Path(str(val))
        if not user_img.is_absolute():
            user_img = (Path.cwd() / user_img).resolve()
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

    save_docx_xml(root, template, out_path, photos=photos, skip_media=skip_media)

    print(json.dumps({
        "ok": True,
        "output": str(out_path),
        "template": manifest["id"],
        "replaced": replaced,
        "overflow_risks": overflow,
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
