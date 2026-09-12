# -*- coding: utf-8 -*-
"""t109 预处理组件包（TemplatePackage v2）——P1 探针版。

从原件 template.docx 提取「栏目标题组件 + 可重复条目原型」的语义清单：
- 页面母版：顶部色带(anchor 8)、底部色条(anchor 7)、个人信息区(anchor 0)、照片(anchor 1)
- 栏目 section：标题框 + 图标 + 横线（合成一个标题组件），正文条目原型独立
- 所有几何用 pt、页面左上原点（posV 相对 page，posH 相对 column≈margin=42pt）

坐标基准（P0 基线实测，Word 16.0.20326 渲染）：
- anchor posV → 渲染标题顶 = posV + 10.55pt（标题框内 tIns 3.6 + 字体行位偏移）
- 正文行距精确 18.0pt（微软雅黑 10.5pt，snapToGrid=0 单倍行距）
- 栏目间距（anchor 间距）= 15.45pt

本模块只读原件；运行时以副本为宿主。包数据为纯 JSON（可 diff、可审核）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

EMU_PER_PT = 12700
# t109 页面几何（sectPr: 11906x16838 twips, margin 720 twips）
PAGE_W_PT = 595.3
PAGE_H_PT = 841.9
MARGIN_PT = 36.0
# 渲染基线：正文文字左缘 = 42pt（column 相对 posH -1.6 + body wsp off 0 + lIns 7.2 + 段缩进差）
CONTENT_X_PT = 42.0
BODY_W_PT = 532.5          # 正文框宽（anchor 2-6 统一）
BODY_USABLE_W_PT = 518.1   # 减左右 insets（91440 EMU = 7.2pt ×2）
TITLE_BOX_H_PT = 30.2      # 标题框高（含 spAutoFit 但不变）
TITLE_TEXT_TOP_OFF = 10.55 # anchor 顶 → 标题文字渲染顶（实测均值）
BODY_FIRST_LINE_OFF = 36.5 # anchor 顶 → 正文首行文字渲染顶（实测 226.4-193.35=33.05? 实为 35.85? 用渲染对照表校准）
LINE_PITCH_PT = 18.0       # 微软雅黑 10.5pt 单倍行距（实测精确值）
BODY_TINS_PT = 3.6
SECTION_GAP_PT = 15.45     # 栏目间隔（anchor 间距）
# 底部装饰条上缘（anchor 7 posV=833.6）；正文不得侵入
FOOTER_BAR_TOP_PT = 833.6
# 第一可用栏目锚点（个人信息区底 + 照片区底较大者 + 间距）
FIRST_SECTION_TOP_PT = 193.35   # 教育 anchor posV（基线）
# 照片区底部：anchor1 posV=60 + 111 = 171pt；个人信息 anchor0 底 = 50.7+127.2=177.9
HEADER_BOTTOM_PT = 177.9

SCHEMA_VERSION = "p1-probe-1"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class EntryPrototype:
    """可重复条目原型：实习/校园/项目经历正文框的克隆源。"""
    proto_id: str
    source_anchor_idx: int   # 原件 anchor 序号（预处理定位用，非运行时 ID）
    body_wsp_idx: int        # anchor 内 wsp 序号
    width_pt: float
    line_pitch_pt: float
    # 首行结构槽位（表格化对齐：日期/机构/角色 = 空格对齐的三个 run 组）
    head_slots: list[str] = field(default_factory=lambda: ["date", "org", "role"])
    body_para_style: dict = field(default_factory=dict)


@dataclass
class SectionTitlePrototype:
    """栏目标题组件：标题框 + 图标 + 横线（同一 anchor 组合）。"""
    proto_id: str
    source_anchor_idx: int
    title_text: str          # 原标题文字（复制栏目时替换）
    box_h_pt: float
    # 标题 anchor 顶 → 正文首条 anchor 顶的固定差（正文 wsp off.y 29.2 - anchor 顶）
    body_gap_pt: float = 29.2


@dataclass
class TemplatePackage:
    template_id: str
    source_path: Path
    source_sha256: str
    schema_version: str
    sections: list[dict]     # 原件既有栏目（语义顺序，自上而下）
    title_proto: SectionTitlePrototype
    entry_proto: EntryPrototype
    master_anchors: dict     # 页面母版 anchor 索引（first page）
    page_geom: dict

    def to_json_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "source_sha256": self.source_sha256,
            "schema_version": self.schema_version,
            "renderer_baseline": "Word 16.0.20326 (COM ExportAsFixedFormat)",
            "page_geom": self.page_geom,
            "sections": self.sections,
            "title_proto": {
                "proto_id": self.title_proto.proto_id,
                "source_anchor_idx": self.title_proto.source_anchor_idx,
                "title_text": self.title_proto.title_text,
                "box_h_pt": self.title_proto.box_h_pt,
                "body_gap_pt": self.title_proto.body_gap_pt,
            },
            "entry_proto": {
                "proto_id": self.entry_proto.proto_id,
                "source_anchor_idx": self.entry_proto.source_anchor_idx,
                "body_wsp_idx": self.entry_proto.body_wsp_idx,
                "width_pt": self.entry_proto.width_pt,
                "line_pitch_pt": self.entry_proto.line_pitch_pt,
                "head_slots": self.entry_proto.head_slots,
            },
            "master_anchors": self.master_anchors,
        }


def build_t109_package(templates_root: Path) -> TemplatePackage:
    """从 t109 原件构建 P1 组件包（只读原件，hash 冻结）。"""
    tpl_dir = templates_root / "t109"
    src = tpl_dir / "template.docx"
    if not src.is_file():
        raise FileNotFoundError(src)
    source_sha = sha256_file(src)
    # 原件栏目（P0 解析结果；anchor 索引仅预处理期使用）
    sections = [
        {"id": "education", "title": "教育背景（Education）", "anchor_idx": 2,
         "origin_pos_v": 193.35, "orig_body_h_pt": 80.0},
        {"id": "internship", "title": "实习经历（Internship）", "anchor_idx": 6,
         "origin_pos_v": 318.0, "orig_body_h_pt": 157.2},
        {"id": "campus", "title": "校园经历（Campus）", "anchor_idx": 5,
         "origin_pos_v": 519.9, "orig_body_h_pt": 98.0},
        {"id": "skills", "title": "技能证书（Skills certificate）", "anchor_idx": 4,
         "origin_pos_v": 644.5, "orig_body_h_pt": 62.0},
        {"id": "summary", "title": "自我评价（Self-assessment）", "anchor_idx": 3,
         "origin_pos_v": 751.1, "orig_body_h_pt": 43.9},
    ]
    return TemplatePackage(
        template_id="t109",
        source_path=src,
        source_sha256=source_sha,
        schema_version=SCHEMA_VERSION,
        sections=sections,
        title_proto=SectionTitlePrototype(
            proto_id="section_title_v1",
            source_anchor_idx=2,     # 以教育栏为原型（标题+图标+横线组合）
            title_text="教育背景（Education）",
            box_h_pt=TITLE_BOX_H_PT,
            body_gap_pt=29.2,
        ),
        entry_proto=EntryPrototype(
            proto_id="entry_v1",
            source_anchor_idx=6,     # 以实习正文为原型（内容最丰富）
            body_wsp_idx=1,
            width_pt=BODY_W_PT,
            line_pitch_pt=LINE_PITCH_PT,
        ),
        master_anchors={
            "top_band": 8, "footer_bar": 7, "personal_info": 0, "photo": 1,
        },
        page_geom={
            "page_w_pt": PAGE_W_PT, "page_h_pt": PAGE_H_PT,
            "margin_pt": MARGIN_PT, "content_x_pt": CONTENT_X_PT,
            "body_w_pt": BODY_W_PT, "body_usable_w_pt": BODY_USABLE_W_PT,
            "line_pitch_pt": LINE_PITCH_PT,
            "section_gap_pt": SECTION_GAP_PT,
            "first_section_top_pt": FIRST_SECTION_TOP_PT,
            "footer_bar_top_pt": FOOTER_BAR_TOP_PT,
            "body_first_line_off": BODY_FIRST_LINE_OFF,
        },
    )


def save_package_json(pkg: TemplatePackage, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(pkg.to_json_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
