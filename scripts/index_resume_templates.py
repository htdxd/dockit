# -*- coding: utf-8 -*-
"""index_resume_templates.py — 简历模板入库标注工具（开发期）

用法:
  python scripts/index_resume_templates.py <docx路径> <输出manifest.json路径> [--id t001] [--name "模板名"]

功能:
  1. 解析 docx 中所有 wp:anchor / grpSp / wsp 文本框，提取文本、字号、bbox
  2. 文本框内按"锚点关键词"切分：一个文本框可含多个字段（如"姓 名：X 年 龄：Y"）
  3. 内容相同的文本框（前后景/副本层）合并为同一字段的多个 location
  4. 无锚点的内容框列 unmapped，并按框内关键词给出疑似归类提示
  5. 生成 manifest 草稿，供人工抽检后入库

标注质量说明:
  自动标注是启发式，入库前必须人工抽检 fields 的 anchor/original/block 标记。
  unmapped 中的文本框需人工决定补标 key 或忽略。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from zipfile import ZipFile

from lxml import etree

# ---------- 命名空间 ----------
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
WPG = "{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}"

EMU_PER_PT = 12700


def emu2pt(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return round(int(v) / EMU_PER_PT, 1)
    except ValueError:
        return None


# ---------- 字段锚点规则（按优先级，关键词可含空格被自动忽略） ----------
# 内容块字段（整块替换）与单值字段分开定义
ANCHOR_RULES: list[tuple[str, list[str]]] = [
    ("name", ["姓    名", "姓名", "Name"]),
    ("phone", ["电    话", "电话", "手    机", "手机", "Phone", "Tel", "Mobile", "联系电话"]),
    ("email", ["邮    箱", "邮箱", "Email", "E-mail", "E-mail地址", "电子邮箱"]),
    ("birth", ["出生年月", "出生日期", "出生", "生日", "Date of Birth"]),
    ("gender", ["性    别", "性别", "Gender"]),
    ("politics", ["政治面貌", "政治", "党员", "Political"]),
    ("address", ["住    址", "地址", "现居", "Address", "Location"]),
    ("website", ["个人主页", "个人网站", "博客", "Website", "GitHub", "Github"]),
    ("intent", ["求职意向", "应聘岗位", "意向岗位", "目标岗位", "Objective", "Job Objective"]),
    ("education", ["教育背景", "教育经历", "Education", "EDUCATION"]),
    ("work", ["工作经历", "工作经验", "实习经历", "Work Experience", "Experience", "EMPLOYMENT"]),
    ("project", ["项目经历", "项目经验", "Project", "Projects"]),
    ("skill", ["专业技能", "技能特长", "职业技能", "个人技能", "Skills", "Skill"]),
    ("language", ["语言能力", "Languages", "Language"]),
    ("summary", ["自我评价", "个人评价", "自我简介", "个人简介", "Summary", "About Me", "About me", "PROFILE", "Profile"]),
    ("certificate", ["证书", "荣誉", "获奖", "Certificates", "Honors", "Awards"]),
    ("title", ["个人简历", "求职简历", "RESUME", "Curriculum Vitae"]),
]

BLOCK_FIELDS = {"education", "work", "project", "skill", "summary", "certificate", "language"}

# 无锚点文本框的疑似归类关键词
CONTENT_HINTS: list[tuple[str, list[str]]] = [
    ("education", ["大学", "学院", "本科", "硕士", "博士", "学士"]),
    ("work", ["有限公司", "科技", "公司", "集团", "任职", "担任"]),
    ("skill", ["证书", "熟练掌握", "熟练", "技能", "语言"]),
    ("project", ["项目", "系统", "平台"]),
    ("certificate", ["获得", "奖", "证书"]),
]


def normalize(s: str) -> str:
    """去所有空白字符"""
    return re.sub(r"\s+", "", s)


def iter_textboxes(root: etree._Element):
    """遍历所有文本框，yield (anchor_idx, wsp_idx, txbx, text, width_pt, height_pt, font_size)

    text 按段落拼接，段间以 \\n 分隔（保留文本框内部行结构）。
    """
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
            if not text.strip():
                continue
            w_pt, h_pt = _shape_bbox(wsp, a_w, a_h)
            sz = _shape_font_size(tx)
            yield a_idx, wsp_idx, tx, text, w_pt, h_pt, sz


def _shape_bbox(wsp, a_w, a_h):
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
    return round(int(ext.get("cx")) / EMU_PER_PT * sx, 1), round(int(ext.get("cy")) / EMU_PER_PT * sy, 1)


def _shape_font_size(tx):
    for r in tx.iter(W + "r"):
        rpr = r.find(W + "rPr")
        if rpr is not None:
            sz = rpr.find(W + "sz")
            if sz is not None and sz.get(W + "val"):
                return round(int(sz.get(W + "val")) / 2, 1)
    return None


def _kw_regex(kw: str):
    """关键词 → 正则：允许关键词内部任意空白（如 '姓    名' 匹配 '姓 名'）"""
    return re.compile(r"\s*".join(re.escape(c) for c in kw if not c.isspace()))


def scan_fields_in_text(text: str) -> list[dict]:
    """在文本框全文内扫描锚点，返回 [{key, anchor, value, block, is_header}]

    逻辑: 所有规则锚点在原文中命中 → 按位置排序 → 每个锚点的值区间
    = [锚点结束, 下一个锚点开始)，单值字段截断到行尾。
    同一位置多个关键词命中 → 取规则顺序靠前的 key。
    """
    hits = []
    key_order = [k for k, _ in ANCHOR_RULES]
    for key, keywords in ANCHOR_RULES:
        for kw in keywords:
            rx = _kw_regex(kw)
            for m in rx.finditer(text):
                # 锚点 = 关键词匹配 + 紧跟冒号（若有）；要求锚点至少含关键词本身
                seg = text[m.start():]
                cm = re.match(r"[^：:]{1,16}[：:]", seg)   # 关键词…冒号
                anchor = cm.group(0) if cm else m.group(0)
                hits.append({"start": m.start(), "key": key, "anchor": anchor})
    if not hits:
        return []
    # 同位置多命中：保留 key 规则靠前
    hits.sort(key=lambda h: (h["start"], key_order.index(h["key"])))
    dedup = []
    for h in hits:
        if dedup and dedup[-1]["start"] == h["start"]:
            continue
        dedup.append(h)

    fields = []
    for i, h in enumerate(dedup):
        anchor_end = h["start"] + len(h["anchor"])
        next_start = dedup[i + 1]["start"] if i + 1 < len(dedup) else None
        # 值区间：锚点后 → 下一个锚点前；块字段跨行保留全文，单值字段截断到行尾
        raw = text[anchor_end:next_start if next_start is not None else len(text)]
        block = h["key"] in BLOCK_FIELDS
        first_line = raw.split("\n")[0].strip() if raw else ""
        value = raw.strip() if block else first_line
        fields.append({
            "key": h["key"],
            "anchor": h["anchor"],
            "value": value,
            "block": block,
            "is_header": block and not value,
        })
    return fields


def content_hint(text: str) -> str | None:
    norm = normalize(text)
    for key, keywords in CONTENT_HINTS:
        for kw in keywords:
            if normalize(kw) in norm:
                return key
    return None


def detect_field(text: str) -> str | None:
    """对整段文本检测是否含某字段锚点（用于单行纯标题分类）"""
    hits = scan_fields_in_text(text)
    return hits[0]["key"] if hits else None


def classify_box(text: str) -> dict | None:
    """文本框级分类（v2 模型）：
    - 分区标题框（纯标题、无值）→ 返回 {"kind": "header"}（不替换，保留原样）
    - 内容块框（含多行/段落）→ 按首行锚点返回 {"kind": "block", "key": ...}
    - 基本信息行组框（每行一个 标签：值）→ 返回 {"kind": "lines", "lines": [...]}
    返回 None 表示无法归类（装饰文本等）。
    """
    lines = text.split("\n")
    non_empty = [ln for ln in lines if ln.strip()]

    # 1) 单行纯标题 → header（分区标题/文档标题/装饰语）
    if len(non_empty) == 1:
        first = detect_field(text)
        if first and first not in BLOCK_FIELDS:
            return {"kind": "header", "key": first}
        return None  # 无法归类（可能是标题/装饰，保守忽略）

    # 2) 多行且**至少 2 个不同行**含单值锚点 → lines 行组（基本信息）
    #    只统计单值字段：内容块字段（language/certificate/skill…）不参与行拆
    anchored_lines = []
    for ln in non_empty:
        fs = scan_fields_in_text(ln)
        single = [f for f in fs if f["key"] not in BLOCK_FIELDS]
        if single:
            anchored_lines.append(single[0])
    if len(anchored_lines) >= 2:
        return {"kind": "lines", "lines": anchored_lines}

    # 3) 首行含锚点（内容块标题行）→ block
    first_field = scan_fields_in_text(non_empty[0])
    if first_field:
        return {"kind": "block", "key": first_field[0]["key"]}

    # 4) 首行无锚点但疑似内容块 → 用内容提示
    hint = content_hint(text)
    if hint and hint in BLOCK_FIELDS:
        return {"kind": "block", "key": hint}
    return None


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    docx_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    tpl_id = tpl_name = None
    for i, arg in enumerate(sys.argv):
        if arg == "--id" and i + 1 < len(sys.argv):
            tpl_id = sys.argv[i + 1]
        if arg == "--name" and i + 1 < len(sys.argv):
            tpl_name = sys.argv[i + 1]

    with ZipFile(docx_path) as z:
        if "word/document.xml" not in z.namelist():
            print(f"[错误] {docx_path.name} 不是有效 docx（缺 word/document.xml）", file=sys.stderr)
            sys.exit(1)
        root = etree.fromstring(z.read("word/document.xml"))

    tpl_id = tpl_id or f"t{docx_path.stem[:8]}"
    tpl_name = tpl_name or docx_path.stem

    # 收集文本框（去重：同 anchor_idx+wsp_idx 只记一次）
    boxes = []
    seen_box = set()
    for a_idx, wsp_idx, tx, text, w, h, sz in iter_textboxes(root):
        key_box = (a_idx, wsp_idx)
        if key_box in seen_box:
            continue
        seen_box.add(key_box)
        boxes.append({"anchor_idx": a_idx, "wsp_idx": wsp_idx, "text": text,
                      "width_pt": w, "height_pt": h, "font_size_pt": sz})

    # v2 字段：line 按行 / block 按块，按 text 全文匹配替换
    fields: list[dict] = []
    unmapped: list[dict] = []
    # 同 key 的 block 多实例计数（work_0, work_1…）
    block_counter: dict[str, int] = {}

    for b in boxes:
        text = b["text"]
        cls = classify_box(text)
        if cls is None:
            unmapped.append({
                "text": text[:70], "width_pt": b["width_pt"], "height_pt": b["height_pt"],
                "anchor_idx": b["anchor_idx"], "wsp_idx": b["wsp_idx"],
                "hint": "未归类，需人工判断（分区标题/装饰语可忽略）",
            })
            continue
        if cls["kind"] == "header":
            continue  # 分区标题/文档标题：保留原样，不入 fields
        if cls["kind"] == "lines":
            # 基本信息行组：每行一个 line 字段
            for f in cls["lines"]:
                fields.append({
                    "id": f"{f['key']}",
                    "key": f["key"],
                    "mode": "line",
                    "text": text.split("\n")[0] if False else _line_text(text, f["anchor"]),
                    "locations": [{
                        "anchor_idx": b["anchor_idx"], "wsp_idx": b["wsp_idx"],
                        "width_pt": b["width_pt"], "height_pt": b["height_pt"],
                        "font_size_pt": b["font_size_pt"],
                    }],
                })
            continue
        # block：整个文本框一个字段
        key = cls["key"]
        n = block_counter.get(key, 0)
        block_counter[key] = n + 1
        fields.append({
            "id": f"{key}_{n}" if n > 0 else key,
            "key": key,
            "mode": "block",
            "text": text,
            "locations": [{
                "anchor_idx": b["anchor_idx"], "wsp_idx": b["wsp_idx"],
                "width_pt": b["width_pt"], "height_pt": b["height_pt"],
                "font_size_pt": b["font_size_pt"],
            }],
        })

    # 合并同 (key, mode, text) 的重复框（前后景副本）→ 多 locations
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for f in fields:
        mk = (f["key"], f["mode"], f["text"])
        if mk not in merged:
            merged[mk] = f
            order.append(mk)
        else:
            merged[mk]["locations"].append(f["locations"][0])
    out_fields = [merged[k] for k in order]

    manifest = {
        "id": tpl_id,
        "name": tpl_name,
        "source": "ResumeCollection (MIT)",
        "template_file": "template.docx",
        "fields": out_fields,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 已生成 manifest: {out_path}")
    print(f"   文本框: {len(boxes)} | 字段: {len(out_fields)} | 未归类: {len(unmapped)}")
    print(f"\n== 字段 ==")
    for f in out_fields:
        loc = f["locations"][0]
        print(f"  {f['id']:<14}[{f['mode']}] x{len(f['locations'])} "
              f"框{loc.get('width_pt')}x{loc.get('height_pt')}pt 字号{loc.get('font_size_pt')}pt")
        for ln in f["text"].split("\n")[:2]:
            print(f"      | {ln[:50]}")
    if unmapped:
        print(f"\n== 未归类（人工判断：分区标题/装饰语可忽略，内容块补标 key）==")
        for u in unmapped:
            print(f"  a{u['anchor_idx']}/w{u['wsp_idx']} | 框{u['width_pt']}x{u['height_pt']}pt | {u['text']!r} | {u['hint']}")


def _line_text(text: str, anchor: str) -> str:
    """返回包含 anchor 的那一行完整文本"""
    for ln in text.split("\n"):
        if anchor in ln:
            return ln.strip()
    return anchor


if __name__ == "__main__":
    main()
