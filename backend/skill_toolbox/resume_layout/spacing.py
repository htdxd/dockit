# -*- coding: utf-8 -*-
"""spacing.py — 原件间距档案（P2-1）。

R3 review 要求：**分别记录文字实际边界、组件外框和栏目间距**，只在真实内容
碰撞/越界时推开；保留实高测量，不恢复不安全的固定框高。

三层几何口径（全部 pt，页面左上原点）：
1. 文字实际边界（ink）：栏内非空段落的首段行顶 → 末段行顶 + 行数×行距；
2. 组件外框（frame）：anchor posV（=标题框顶）→ anchor posV + 正文框 off.y + 正文框 ext.cy；
3. 栏目间距：相邻两栏 frame 的差（可为负——原件框内有空白时外框会互相交叠）。

布局消费量是 `anchor_delta_from_prev_text_pt = next.frame_top - prev.text_ink_bottom`：
内容与原件一致时它精确复现原件 anchor 位置；内容变长时后一栏随文字底同步下移
（不依赖固定框高，不做图像式缩放）。

数据来源：t109 原件副本 + Word COM（Information(6) 逐段行位 / ComputeStatistics(1)
行数）+ document.xml 几何；`build_archive_from_probe` 是纯函数，单测可注入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from lxml import etree

EMU_PER_PT = 12700.0
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"

SCHEMA_VERSION = "spacing-archive-1"
TITLE_TEXT_TOP_OFF_PT = 10.55   # frame_top → 标题文字渲染顶（模板包同值）
LINE_PITCH_PT = 18.0

from skill_toolbox.resume_layout.t109 import (
    SOURCE_SECTION_NAMES as SECTION_ANCHOR_NAMES,
    MEASURE_SHAPE_BY_SECTION as SECTION_SHAPE_INDEX,
)
SECTION_ORDER = list(SECTION_ANCHOR_NAMES)


@dataclass
class SectionSpacing:
    section_id: str
    frame_top_pt: float
    frame_bottom_pt: float
    body_wsp_off_y_pt: float
    body_h_pt: float
    text_ink_top_pt: float
    text_ink_bottom_pt: float
    content_paragraphs: int
    content_lines: int
    bottom_slack_pt: float
    entry_count: int = 1
    # 条目间距：上一条文字块底 → 下一条文字块顶（原件用空段分隔条目，
    # 如实习栏 p3 是 sz=6 的 3pt 空段，实测 5.2pt）。单条目栏为 None。
    entry_gap_pt: float | None = None
    # 每条目行数（档案自证用：可用它重放原件内容验证 anchor 复现）
    entry_lines: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "frame_top_pt": round(self.frame_top_pt, 2),
            "frame_bottom_pt": round(self.frame_bottom_pt, 2),
            "body_wsp_off_y_pt": round(self.body_wsp_off_y_pt, 2),
            "body_h_pt": round(self.body_h_pt, 2),
            "text_ink_top_pt": round(self.text_ink_top_pt, 2),
            "text_ink_bottom_pt": round(self.text_ink_bottom_pt, 2),
            "bottom_slack_pt": round(self.bottom_slack_pt, 2),
            "content_paragraphs": self.content_paragraphs,
            "content_lines": self.content_lines,
            "entry_count": self.entry_count,
            "entry_gap_pt": (
                None if self.entry_gap_pt is None else round(self.entry_gap_pt, 2)
            ),
            "entry_lines": list(self.entry_lines),
        }


@dataclass
class PairSpacing:
    """相邻两栏：外框间距 / 可见间距 / 布局用的 anchor→文字底增量。"""

    prev_section_id: str
    next_section_id: str
    anchor_gap_pt: float
    visible_gap_pt: float
    anchor_delta_from_prev_text_pt: float

    def to_dict(self) -> dict:
        return {
            "prev_section_id": self.prev_section_id,
            "next_section_id": self.next_section_id,
            "anchor_gap_pt": round(self.anchor_gap_pt, 2),
            "visible_gap_pt": round(self.visible_gap_pt, 2),
            "anchor_delta_from_prev_text_pt": round(
                self.anchor_delta_from_prev_text_pt, 2
            ),
        }


@dataclass
class SpacingArchive:
    template_id: str
    template_sha256: str
    schema_version: str
    title_text_top_off_pt: float
    sections: dict[str, SectionSpacing] = field(default_factory=dict)
    pairs: dict[tuple[str, str], PairSpacing] = field(default_factory=dict)

    def delta_after(self, prev_id: str, next_id: str | None, default: float) -> float:
        """布局用：上一栏文字底 → 本栏 frame_top 的增量。

        未登记的有序对回落 `default`（= 正文框底余量 + 通用栏目间距）。
        """
        if next_id is None:
            return default
        pair = self.pairs.get((prev_id, next_id))
        return default if pair is None else pair.anchor_delta_from_prev_text_pt

    def entry_gap_for(self, section_id: str, default: float) -> float:
        """布局用：同栏条目间距（上一条文字底 → 下一条文字顶）。

        原件用空段分隔条目才有登记值（如实习栏 5.2pt）；未登记回落
        `default`（= 首行偏移 + 框底余量 = P1 行为）。
        """
        sec = self.sections.get(section_id)
        if sec is None or sec.entry_gap_pt is None:
            return default
        return sec.entry_gap_pt

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "template_id": self.template_id,
            "template_sha256": self.template_sha256,
            "title_text_top_off_pt": self.title_text_top_off_pt,
            "line_pitch_pt": LINE_PITCH_PT,
            "sections": [s.to_dict() for s in self.sections.values()],
            "pairs": [p.to_dict() for p in self.pairs.values()],
        }


def build_archive_from_probe(
    probed: dict,
    *,
    template_id: str,
    template_sha256: str,
    title_text_top_off_pt: float = TITLE_TEXT_TOP_OFF_PT,
) -> SpacingArchive:
    """纯函数：COM 逐段数据 + XML 外框数据 → SpacingArchive。

    probed: {
      "frames": {section_id: {"frame_top_pt":..,"body_wsp_off_y_pt":..,"body_h_pt":..}},
      "paragraphs": {section_id: [{"top_pt":..,"lines":..,"text":..}, ...]},
      "order": [section_id, ...],
    }
    """
    frames = probed["frames"]
    paras = probed["paragraphs"]
    order = probed.get("order") or list(frames)
    sections: dict[str, SectionSpacing] = {}
    for sid in order:
        fr = frames[sid]
        raw = list(paras.get(sid, []))
        content = [p for p in raw if (p.get("text") or "").strip()]
        if not content:
            raise ValueError(f"栏目 {sid} 原件无内容段落，无法建立间距档案")
        lines = sum(int(p["lines"]) for p in content)
        ink_top = float(content[0]["top_pt"])
        ink_bottom = float(content[-1]["top_pt"]) + int(content[-1]["lines"]) * LINE_PITCH_PT
        frame_top = float(fr["frame_top_pt"])
        frame_bottom = frame_top + float(fr["body_wsp_off_y_pt"]) + float(fr["body_h_pt"])
        # 条目边界：原件用**空段**分隔同栏条目（实习栏 p3 = sz=6 的 3pt 空段）。
        # 相邻条目间距 = 下一条文字顶 − 上一条文字底。
        gaps: list[float] = []
        entry_lines: list[int] = []
        cur_lines = 0
        prev_bottom: float | None = None
        pending_empty = False
        for p in raw:
            if not (p.get("text") or "").strip():
                if prev_bottom is not None:
                    pending_empty = True
                continue
            if prev_bottom is not None:
                if pending_empty:
                    gaps.append(float(p["top_pt"]) - prev_bottom)
                    entry_lines.append(cur_lines)
                    cur_lines = 0
                pending_empty = False
            cur_lines += int(p["lines"])
            prev_bottom = float(p["top_pt"]) + int(p["lines"]) * LINE_PITCH_PT
        entry_lines.append(cur_lines)
        sections[sid] = SectionSpacing(
            section_id=sid,
            frame_top_pt=frame_top,
            frame_bottom_pt=frame_bottom,
            body_wsp_off_y_pt=float(fr["body_wsp_off_y_pt"]),
            body_h_pt=float(fr["body_h_pt"]),
            text_ink_top_pt=ink_top,
            text_ink_bottom_pt=ink_bottom,
            bottom_slack_pt=frame_bottom - ink_bottom,
            content_paragraphs=len(content),
            content_lines=lines,
            entry_count=len(entry_lines),
            entry_gap_pt=(min(gaps) if gaps else None),
            entry_lines=entry_lines,
        )
    pairs: dict[tuple[str, str], PairSpacing] = {}
    for prev_id, next_id in pairwise(order):
        prev, nxt = sections[prev_id], sections[next_id]
        pairs[(prev_id, next_id)] = PairSpacing(
            prev_section_id=prev_id,
            next_section_id=next_id,
            anchor_gap_pt=nxt.frame_top_pt - prev.frame_bottom_pt,
            visible_gap_pt=(
                nxt.frame_top_pt + title_text_top_off_pt - prev.text_ink_bottom_pt
            ),
            anchor_delta_from_prev_text_pt=nxt.frame_top_pt - prev.text_ink_bottom_pt,
        )
    return SpacingArchive(
        template_id=template_id,
        template_sha256=template_sha256,
        schema_version=SCHEMA_VERSION,
        title_text_top_off_pt=title_text_top_off_pt,
        sections=sections,
        pairs=pairs,
    )


# ---------------- 原件探针（Windows + Word COM） ----------------

def _frame_geometry(template: Path) -> dict[str, dict]:
    import zipfile

    with zipfile.ZipFile(template) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    out: dict[str, dict] = {}
    by_name = {v: k for k, v in SECTION_ANCHOR_NAMES.items()}
    for anchor in root.iter(WP + "anchor"):
        dp = anchor.find(WP + "docPr")
        name = dp.get("name") if dp is not None else None
        sid = by_name.get(name or "")
        if sid is None:
            continue
        pv = anchor.find(WP + "positionV/" + WP + "posOffset")
        if pv is None or pv.text is None:
            raise RuntimeError(f"{name}: 缺 positionV")
        body_wsp = None
        for wsp in anchor.iter(WPS + "wsp"):
            tx = wsp.find(".//" + W + "txbxContent")
            if tx is None:
                continue
            ps = list(tx.iter(W + "p"))
            text = "".join(
                "".join(t.text or "" for t in p.iter(W + "t")) for p in ps
            )
            if text.strip() and len(text) < 40 and ("（" in text or "(" in text):
                continue  # 标题框（短文本 + 全角括号）
            sp = wsp.find(WPS + "spPr")
            xf = sp.find(A + "xfrm") if sp is not None else None
            cy = int(xf.find(A + "ext").get("cy")) if xf is not None and xf.find(A + "ext") is not None else 0
            key = (len(ps), cy)
            if body_wsp is None or key > body_wsp[0]:
                body_wsp = (key, wsp, ps)
        if body_wsp is None:
            raise RuntimeError(f"{name}: 找不到正文框")
        _, body_wsp_el, _ps = body_wsp
        sp = body_wsp_el.find(WPS + "spPr")
        xf = sp.find(A + "xfrm")
        off = xf.find(A + "off")
        ext = xf.find(A + "ext")
        out[sid] = {
            "frame_top_pt": int(pv.text) / EMU_PER_PT,
            "body_wsp_off_y_pt": int(off.get("y") or 0) / EMU_PER_PT,
            "body_h_pt": int(ext.get("cy")) / EMU_PER_PT,
        }
    missing = [s for s in SECTION_ORDER if s not in out]
    if missing:
        raise RuntimeError(f"原件缺栏目 anchor：{missing}")
    return out


def _paragraph_geometry(host: Path) -> dict[str, list[dict]]:
    from skill_toolbox.word_com import start_word, close_word

    out: dict[str, list[dict]] = {}
    word = start_word()
    doc = None
    try:
        doc = word.Documents.Open(str(host), False, True)
        for sid, idx in SECTION_SHAPE_INDEX.items():
            tr = doc.Shapes(idx).GroupItems(2).TextFrame.TextRange
            rows = []
            for i in range(1, tr.Paragraphs.Count + 1):
                rng = tr.Paragraphs(i).Range
                rows.append({
                    "top_pt": float(rng.Information(6)),
                    "lines": int(rng.ComputeStatistics(1)),
                    "text": (rng.Text or "").replace("\r", ""),
                })
            out[sid] = rows
    finally:
        close_word(word, doc)
    return out


def probe_template(template: Path, *, template_id: str = "t109") -> SpacingArchive:
    """在原件**副本**上探针（原件只读），返回间距档案。"""
    import hashlib
    import shutil
    import tempfile

    sha = hashlib.sha256(template.read_bytes()).hexdigest()
    tmp = Path(tempfile.mkdtemp(prefix="t109_spacing_"))
    try:
        host = tmp / "t109_copy.docx"
        shutil.copy(template, host)
        probed = {
            "frames": _frame_geometry(template),
            "paragraphs": _paragraph_geometry(host),
            "order": SECTION_ORDER,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return build_archive_from_probe(
        probed, template_id=template_id, template_sha256=sha
    )


def load_archive(path: Path) -> SpacingArchive:
    raw = json.loads(path.read_text(encoding="utf-8"))
    archive = SpacingArchive(
        template_id=raw["template_id"],
        template_sha256=raw["template_sha256"],
        schema_version=raw["schema_version"],
        title_text_top_off_pt=raw["title_text_top_off_pt"],
    )
    for s in raw["sections"]:
        archive.sections[s["section_id"]] = SectionSpacing(
            section_id=s["section_id"],
            frame_top_pt=s["frame_top_pt"],
            frame_bottom_pt=s["frame_bottom_pt"],
            body_wsp_off_y_pt=s["body_wsp_off_y_pt"],
            body_h_pt=s["body_h_pt"],
            text_ink_top_pt=s["text_ink_top_pt"],
            text_ink_bottom_pt=s["text_ink_bottom_pt"],
            bottom_slack_pt=s["bottom_slack_pt"],
            content_paragraphs=s["content_paragraphs"],
            content_lines=s["content_lines"],
            entry_count=s.get("entry_count", 1),
            entry_gap_pt=s.get("entry_gap_pt"),
            entry_lines=list(s.get("entry_lines") or []),
        )
    for p in raw["pairs"]:
        archive.pairs[(p["prev_section_id"], p["next_section_id"])] = PairSpacing(
            prev_section_id=p["prev_section_id"],
            next_section_id=p["next_section_id"],
            anchor_gap_pt=p["anchor_gap_pt"],
            visible_gap_pt=p["visible_gap_pt"],
            anchor_delta_from_prev_text_pt=p["anchor_delta_from_prev_text_pt"],
        )
    return archive


def save_archive(archive: SpacingArchive, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(archive.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_archive(
    template: Path, path: Path, *, template_id: str = "t109", rebuild: bool = False
) -> SpacingArchive:
    """档案缺失或与当前原件 hash 不符时重建（原件只读）。"""
    import hashlib

    sha = hashlib.sha256(template.read_bytes()).hexdigest()
    if not rebuild and path.is_file():
        try:
            arch = load_archive(path)
        except (KeyError, ValueError, json.JSONDecodeError):
            arch = None
        if arch is not None and arch.template_sha256 == sha:
            return arch
    arch = probe_template(template, template_id=template_id)
    save_archive(arch, path)
    return arch


def main() -> None:
    import sys

    template = Path(sys.argv[1])
    out = Path(sys.argv[2])
    arch = probe_template(template)
    save_archive(arch, out)
    print(json.dumps({"ok": True, "out": str(out), "archive": arch.to_dict()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
