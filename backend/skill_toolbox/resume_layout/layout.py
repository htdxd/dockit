# -*- coding: utf-8 -*-
"""确定性排版器：内容条目 + 真实测量 → LayoutPlan（页、y、高度、分页）。

规则（实施计划 §4.2，P1 探针范围）：
1. 栏目按顺序自上而下排布；每栏 = 标题组件 + N 条条目。
2. 标题与首条绑定：标题 + 首条放不下当前页 → 整体移到下一页（标题不孤悬页底）。
3. 条目完整搬运（不裁剪、不改字号）。
4. **任何放置都必须先校验最终矩形**：条目总占用 = 标题偏移（首条）或 0 +
   正文框高度；翻页后同样校验——放不下整页即 `LAYOUT_UNSATISFIABLE`，
   绝不返回越界计划。
5. 高度全部来自 Word COM 真实测量（EntryMeasurement），估算值只作 sanity check。

布局输出纯数据（LayoutPlan），emit 负责落到 DOCX。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

PAGE_H_PT = 841.9
# 底部装饰条上缘（正文不得侵入；与 template.FOOTER_BAR_TOP_PT 同值）
PAGE_BOTTOM_PT = 833.6
SECTION_GAP_PT = 15.45
TITLE_BOX_H_PT = 30.2
# anchor 顶 → 标题框底（标题框在 anchor 内 off.y=0 高 30.2；正文框 off.y=29.2）
BODY_ANCHOR_OFF_PT = 29.2
# 正文框内首行文字相对正文框顶（tIns 3.6 + 行内偏移 0.25；P0 基线实测）
BODY_FIRST_TEXT_OFF_PT = 3.85
# 条目文字高度 → 正文框高度：首行偏移 + 文字高 + 底部余量（bIns 3.6 + 行降余量 1.0）
BODY_BOTTOM_PAD_PT = 4.6
# 未登记间距对的回落增量 = 正文框底余量 + 通用栏目间距（= P1 行为，向后兼容）
DEFAULT_SECTION_DELTA_PT = BODY_BOTTOM_PAD_PT + SECTION_GAP_PT
# 同栏条目间距回落值（= 首行偏移 + 框底余量，P1 行为）
DEFAULT_ENTRY_GAP_PT = BODY_FIRST_TEXT_OFF_PT + BODY_BOTTOM_PAD_PT
# 条目间距安全下限（防档案异常导致正文相撞）
MIN_ENTRY_GAP_PT = 2.0
# 标题文字顶 → anchor 顶（模板包口径）
TITLE_TEXT_TOP_OFF_PT = 10.55
# 可见间距安全下限：本栏标题文字顶至少要高出上一栏文字底这么多
MIN_VISIBLE_GAP_PT = 8.0
# 跨栏目碰撞下限（标题文字顶 vs 上一栏文字底）：原件最小可见间距为 11.85pt，
# 取 1pt 只拦真实碰撞，不把模板原有紧凑间距误判为错误
MIN_SECTION_GAP_PT = 1.0


class SpacingSource(Protocol):
    """间距档案接口（spacing.SpacingArchive 满足；单测可注入替身）。"""

    def delta_after(
        self, prev_id: str, next_id: str | None, default: float
    ) -> float: ...

    def entry_gap_for(self, section_id: str, default: float) -> float: ...


@dataclass(frozen=True)
class LayoutGeometry:
    page_top_pt: float = 193.35
    page_bottom_pt: float = PAGE_BOTTOM_PT
    title_height_pt: float = TITLE_BOX_H_PT
    body_offset_pt: float = BODY_ANCHOR_OFF_PT
    text_top_pt: float = BODY_FIRST_TEXT_OFF_PT
    body_pad_pt: float = BODY_BOTTOM_PAD_PT
    title_text_top_pt: float = TITLE_TEXT_TOP_OFF_PT
    continuation_page_top_pt: float | None = None



class LayoutUnsatisfiable(Exception):
    """放置校验失败（单条超整页 / 页面无容量）——带 item_id 与原因。"""

    def __init__(self, item_id: str, reason: str) -> None:
        super().__init__(f"LAYOUT_UNSATISFIABLE: {reason} (item_id={item_id})")
        self.item_id = item_id
        self.reason = reason


@dataclass
class EntryPlan:
    instance_id: str
    section_id: str
    page_index: int
    anchor_y_pt: float      # 本条正文框 anchor 的页面 y（posV）
    body_h_pt: float        # 正文框高度（emit 写入 ext cy）
    measured_lines: int
    text_height_pt: float
    # 几何调整的可见说明（如翻页丢弃相对位移），供复核；不用它承载版式决策
    adjusted_note: str = ""
    text_top_offset_pt: float | None = None
    body_width_pt: float | None = None
    body_offset_pt: float | None = None


@dataclass
class SectionPlan:
    section_id: str
    title: str
    page_index: int
    anchor_y_pt: float      # 标题 anchor posV
    entries: list[EntryPlan] = field(default_factory=list)
    gap_after_pt: float | None = None    # 本栏文字底 → 下一栏 anchor 的增量
    gap_source: str = "default"          # archive | default | page_break
    title_text_top_offset_pt: float | None = None


@dataclass
class LayoutPlan:
    pages: int
    sections: list[SectionPlan]
    page_capacity_top_pt: float
    page_capacity_bottom_pt: float
    continuation_page_top_pt: float | None = None

    def to_dict(self) -> dict:
        return {
            "pages": self.pages,
            "page_capacity_top_pt": self.page_capacity_top_pt,
            "page_capacity_bottom_pt": self.page_capacity_bottom_pt,
            "continuation_page_top_pt": self.continuation_page_top_pt,
            "sections": [
                {
                    "section_id": s.section_id,
                    "title": s.title,
                    "page_index": s.page_index,
                    "anchor_y_pt": round(s.anchor_y_pt, 2),
                    "gap_after_pt": (
                        None if s.gap_after_pt is None else round(s.gap_after_pt, 2)
                    ),
                    "gap_source": s.gap_source,
                    "title_text_top_offset_pt": s.title_text_top_offset_pt,
                    "entries": [
                        {
                            "instance_id": e.instance_id,
                            "page_index": e.page_index,
                            "anchor_y_pt": round(e.anchor_y_pt, 2),
                            "body_h_pt": round(e.body_h_pt, 2),
                            "measured_lines": e.measured_lines,
                            "text_height_pt": round(e.text_height_pt, 2),
                            "text_top_offset_pt": e.text_top_offset_pt,
                            "body_width_pt": e.body_width_pt,
                            "body_offset_pt": e.body_offset_pt,
                            **({"adjusted_note": e.adjusted_note}
                               if e.adjusted_note else {}),
                        }
                        for e in s.entries
                    ],
                }
                for s in self.sections
            ],
        }


@dataclass
class MeasureResult:
    """真实测量输入（由 measure.py 提供；单测可注入）。"""
    entry_id: str
    wrapped_lines: int
    text_height_pt: float
    text_top_offset_pt: float | None = None
    body_width_pt: float | None = None
    body_offset_pt: float | None = None
    body_pad_pt: float | None = None

    @classmethod
    def from_dict(cls, row: dict) -> "MeasureResult":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__ if key in row})


def entry_body_height(m: MeasureResult, geometry: LayoutGeometry | None = None) -> float:
    """条目正文框高度 = 首行偏移 + 文字高 + 底部余量。"""
    geometry = geometry or LayoutGeometry()
    return geometry.text_top_pt + m.text_height_pt + geometry.body_pad_pt


def _entry_footprint(k: int, body_h: float, body_offset: float = BODY_ANCHOR_OFF_PT) -> float:
    """条目放置校验用的纵向占用（自候选 y 起算到组件底）。

    k=0（首条，带标题组合）：标题→正文偏移 + 正文高（标题框 0~30.2 与正文框
    29.2~ 在 anchor 内重叠 1pt，实际外沿由正文框决定，故从 y 到组件底 =
    BODY_ANCHOR_OFF_PT + body_h）；
    k>0（strip 后仅正文框）：布局坐标 anchor_y 已是正文框顶语义，占用即 body_h。
    """
    return (body_offset if k == 0 else 0.0) + body_h


def plan_layout(
    sections: list[dict],
    measurements: dict[str, MeasureResult],
    *,
    page_top_pt: float = 193.35,
    page_bottom_pt: float = 833.6,
    page_h_pt: float = PAGE_H_PT,
    section_gap_pt: float = SECTION_GAP_PT,
    spacing: SpacingSource | None = None,
    min_visible_gap_pt: float = MIN_VISIBLE_GAP_PT,
    entry_adjust: dict[str, dict] | None = None,
    geometry: LayoutGeometry | None = None,
) -> LayoutPlan:
    """sections: [{id, title, entries:[{id}]}]（语义顺序）。

    measurements: entry_id → MeasureResult（真实测量）。
    spacing: 原件间距档案（可选）。给定时栏目推进量取档案里该有序对的
        `anchor_delta_from_prev_text_pt`（= 原件 anchor 顶 − 上一栏文字底），
        内容与原件一致时精确复现原件栏位；未登记的有序对回落
        `DEFAULT_SECTION_DELTA_PT`（= 旧行为：正文框底余量 + 通用间距）。
        **不使用固定框高**：条目高度一律来自真实测量。
    entry_adjust: entry_id → 几何约束（P2-R2）。支持的键：
        - `dy_pt`：本条（含其所属栏目标题，若为本栏首条）相对自然流位置下移
          dy（可为负=上移）；**其后所有条目、后续栏目与分页一起重排**；
        - `height_pt`：本条正文框绝对高度；
        - `height_delta_pt`：本条正文框高度增量。
        这样几何约束参与完整 flow 排版，而不是在布局结果上事后平移。
    返回 LayoutPlan；任何放置矩形越界或单条超整页时抛 LayoutUnsatisfiable。
    """
    geometry = geometry or LayoutGeometry(page_top_pt=page_top_pt, page_bottom_pt=page_bottom_pt)
    page_top_pt, page_bottom_pt = geometry.page_top_pt, geometry.page_bottom_pt
    BODY_ANCHOR_OFF_PT = geometry.body_offset_pt
    BODY_FIRST_TEXT_OFF_PT = geometry.text_top_pt
    BODY_BOTTOM_PAD_PT = geometry.body_pad_pt
    TITLE_BOX_H_PT = geometry.title_height_pt
    TITLE_TEXT_TOP_OFF_PT = geometry.title_text_top_pt
    def page_top(page: int) -> float:
        return (geometry.continuation_page_top_pt
                if page > 0 and geometry.continuation_page_top_pt is not None else page_top_pt)

    capacity_h = max(page_bottom_pt - page_top(0), page_bottom_pt - page_top(1))
    if capacity_h <= BODY_ANCHOR_OFF_PT + TITLE_BOX_H_PT:
        raise LayoutUnsatisfiable(
            "_page", f"页面内容区高度 {capacity_h:.1f}pt 不足以放置任何栏目标题+首条"
        )
    adjusts = entry_adjust or {}
    default_delta = section_gap_pt + BODY_BOTTOM_PAD_PT
    default_entry_gap = geometry.text_top_pt + geometry.body_pad_pt
    ordered_ids = [s["id"] for s in sections]
    plans: list[SectionPlan] = []
    page_index = 0
    y = page_top_pt

    def _check_placement(item_id: str, top: float, footprint: float, page: int) -> None:
        """放置终检：矩形底部不得越页底。翻页后 top=page_top 重算同样受限。"""
        if top < page_top(page) - 1e-6:
            raise LayoutUnsatisfiable(item_id, f"条目顶 {top:.1f}pt 超出第 {page + 1} 页内容区上边界")
        bottom = top + footprint
        if bottom > page_bottom_pt + 1e-6:
            raise LayoutUnsatisfiable(
                item_id,
                f"条目放置矩形越界：第 {page+1} 页 top={top:.1f} + 高 {footprint:.1f} "
                f"= 底 {bottom:.1f}pt > 页容量下限 {page_bottom_pt:.1f}pt"
                + ("（单条超过整页容量）" if footprint > capacity_h else "（此处放不下，需上移或精简）"),
            )

    for si, sec in enumerate(sections):
        section_scale = float(sec.get("scale") or 1)
        BODY_ANCHOR_OFF_PT = float(sec.get("body_offset_pt", geometry.body_offset_pt)) * section_scale
        TITLE_BOX_H_PT = geometry.title_height_pt * section_scale
        TITLE_TEXT_TOP_OFF_PT = geometry.title_text_top_pt * section_scale
        entries = sec.get("entries", [])
        next_id = ordered_ids[si + 1] if si + 1 < len(ordered_ids) else None
        # 同栏条目间距（上一条文字底 → 下一条文字顶）：有档案登记值用原件实测
        # 值（实习栏 5.2pt），否则回落首行偏移+框底余量（= P1 行为）
        entry_gap_pt = (
            default_entry_gap
            if spacing is None
            else spacing.entry_gap_for(sec["id"], default_entry_gap)
        )
        entry_gap_pt = max(entry_gap_pt, MIN_ENTRY_GAP_PT)
        sec_title_y: float | None = None
        sec_entries: list[EntryPlan] = []
        cursor: float | None = None
        prev_text_bottom: float | None = None
        dropped_dy = False
        cur_page = page_index
        for k, ent in enumerate(entries):
            m = measurements.get(ent["id"])
            if m is None:
                raise LayoutUnsatisfiable(
                    ent["id"], "缺少真实测量结果（禁止用估算值排布）"
                )
            adj = adjusts.get(ent["id"]) or {}
            dy = float(adj.get("dy_pt", 0.0) or 0.0)
            height_abs = adj.get("height_pt")
            delta_h = float(adj.get("height_delta_pt", 0.0) or 0.0)
            entry_scale = float(ent.get("scale") or 1) * section_scale
            BODY_FIRST_TEXT_OFF_PT = (m.text_top_offset_pt if m.text_top_offset_pt is not None
                                     else geometry.text_top_pt * entry_scale)
            BODY_BOTTOM_PAD_PT = (m.body_pad_pt if m.body_pad_pt is not None
                                  else geometry.body_pad_pt * entry_scale)
            body_h = BODY_FIRST_TEXT_OFF_PT + m.text_height_pt + BODY_BOTTOM_PAD_PT
            if height_abs is not None:
                body_h = float(height_abs)
            elif delta_h:
                body_h = body_h + delta_h
            min_body_h = BODY_FIRST_TEXT_OFF_PT + m.text_height_pt
            if body_h + 1e-6 < min_body_h:
                raise LayoutUnsatisfiable(
                    ent["id"],
                    f"条目正文框被压到 {body_h:.1f}pt，小于文字需要的高度 "
                    f"{min_body_h:.1f}pt（会裁切内容）",
                )
            footprint = _entry_footprint(k, body_h, BODY_ANCHOR_OFF_PT)
            if footprint > capacity_h:
                # 无论放哪页都越界 → 立即失败（含翻页后的情形）
                part = "含标题偏移" if k == 0 else "纯正文框"
                raise LayoutUnsatisfiable(
                    ent["id"],
                    f"单条占用 {footprint:.1f}pt（{part}）超过整页容量 {capacity_h:.1f}pt",
                )
            if cursor is None:
                # 本栏第一条：标题 + 首条一起放；放不下整体去下一页（再校验）
                place = y + dy
                if place < page_top(cur_page) - 1e-6:
                    raise LayoutUnsatisfiable(
                        ent["id"],
                        f"上移后位置 {place:.1f}pt 超出内容区上边界 {page_top(cur_page):.1f}pt",
                    )
                if place + footprint > page_bottom_pt + 1e-6:
                    # 翻页即重置相对位移（计划 §6.1：跨页用页/语义顺序，不靠
                    # 累加 y 模拟分页）；被丢弃的位移**显式记录**，不静默忽略
                    if dy:
                        dropped_dy = True
                    cur_page += 1
                    y = page_top(cur_page)
                    place = y
                _check_placement(ent["id"], place, footprint, cur_page)
                sec_title_y = place
                entry_anchor = place + BODY_ANCHOR_OFF_PT
                cursor = entry_anchor + body_h
            else:
                # 候选 anchor：使本条文字顶距上一条文字底 = entry_gap_pt（+ dy）
                candidate = prev_text_bottom + entry_gap_pt - BODY_FIRST_TEXT_OFF_PT + dy
                if dy < 0 and candidate + BODY_FIRST_TEXT_OFF_PT < (
                    prev_text_bottom + MIN_ENTRY_GAP_PT - 1e-6
                ):
                    raise LayoutUnsatisfiable(
                        ent["id"],
                        f"上移 {abs(dy):.1f}pt 会与上一条文字重叠"
                        f"（间距 {candidate + BODY_FIRST_TEXT_OFF_PT - prev_text_bottom:.1f}pt"
                        f" < {MIN_ENTRY_GAP_PT}pt）",
                    )
                if candidate + body_h > page_bottom_pt + 1e-6:
                    # 条目跨页续排：翻页后仍要校验完整矩形
                    cur_page += 1
                    entry_anchor = page_top(cur_page)
                    _check_placement(ent["id"], entry_anchor, body_h, cur_page)
                    cursor = entry_anchor + body_h
                else:
                    entry_anchor = candidate
                    _check_placement(ent["id"], entry_anchor, body_h, cur_page)
                    cursor = entry_anchor + body_h
            prev_text_bottom = entry_anchor + BODY_FIRST_TEXT_OFF_PT + m.text_height_pt
            sec_entries.append(EntryPlan(
                instance_id=ent["id"],
                section_id=sec["id"],
                page_index=cur_page,
                anchor_y_pt=entry_anchor,
                body_h_pt=body_h,
                measured_lines=m.wrapped_lines,
                text_height_pt=m.text_height_pt,
                text_top_offset_pt=BODY_FIRST_TEXT_OFF_PT,
                body_width_pt=m.body_width_pt,
                body_offset_pt=BODY_ANCHOR_OFF_PT,
                adjusted_note=(
                    "dy_pt 因翻页重置为 0（跨页按页/语义顺序排版）"
                    if dropped_dy else ""
                ),
            ))
        if not entries:
            # 空栏目：只放标题（可选栏目整体消失由上游决定；此处支持纯标题）
            if y + TITLE_BOX_H_PT > page_bottom_pt + 1e-6:
                cur_page += 1
                y = page_top(cur_page)
            if y + TITLE_BOX_H_PT > page_bottom_pt + 1e-6:
                raise LayoutUnsatisfiable(sec["id"], "纯标题栏目也放不下（页面无容量）")
            sec_title_y = y
            y = y + TITLE_BOX_H_PT
            plans.append(SectionPlan(sec["id"], sec.get("title", ""), cur_page, sec_title_y))
            continue
        plans.append(SectionPlan(
            sec["id"], sec.get("title", ""),
            sec_entries[0].page_index,
            sec_title_y if sec_title_y is not None else y, sec_entries,
            title_text_top_offset_pt=TITLE_TEXT_TOP_OFF_PT,
        ))
        # 栏目推进：上一栏「组件外框底」+ 框间距。
        # - 框间距 = 档案增量 − 正文框底余量：原件档案的增量是「anchor 顶 −
        #   上一栏**文字底**」，而文字底到框底有固定余量（BODY_BOTTOM_PAD）；
        #   换算成框底起算后，`prev_box_bottom + (delta - pad)` 在**无几何修改**
        #   时与原实现逐点等值（例：edu 框底 303.0 + (19.6−4.6) = 318.0 = 原件
        #   anchor），同时让「加高正文框」也能推动后续栏目（P2-R2：尺寸约束
        #   参与完整 flow 排版）。
        # - 无档案/未登记：回落 通用间距（gap_source=default）。
        prev_box_bottom = max(
            (e.anchor_y_pt + e.body_h_pt for e in sec_entries
             if e.page_index == cur_page), default=None
        )
        text_bottom = (
            prev_text_bottom if prev_text_bottom is not None
            else cursor - BODY_BOTTOM_PAD_PT
        )
        if next_id is None:
            # 末栏：无后续栏目，不计算（避免最后一段空推进被误读为分页）
            plans[-1].gap_after_pt = None
            plans[-1].gap_source = "none"
            continue
        if spacing is None:
            delta, gap_source = default_delta, "default"
        else:
            delta = spacing.delta_after(sec["id"], next_id, default_delta)
            gap_source = "archive" if delta != default_delta else "default"
        y = (prev_box_bottom if prev_box_bottom is not None else text_bottom) + (
            delta - BODY_BOTTOM_PAD_PT
        )
        # 可见间距兜底：本栏标题文字顶不得压到上一栏文字底（档案里
        # 外框可交叠，但文字不可；只有真实内容碰撞才推开）
        min_y = text_bottom + min_visible_gap_pt - TITLE_TEXT_TOP_OFF_PT
        if y < min_y:
            y, gap_source = min_y, "min_visible_gap"
        plans[-1].gap_after_pt = y - text_bottom
        plans[-1].gap_source = gap_source
        if y > page_bottom_pt:
            page_index = cur_page + 1
            y = page_top(page_index)
            plans[-1].gap_source = f"{gap_source}+page_break"
        else:
            page_index = cur_page

    # 终检：所有放置矩形的底部（计划层面兜底，防任何路径漏检）
    for s in plans:
        for e in s.entries:
            bottom = e.anchor_y_pt + e.body_h_pt
            if bottom > page_bottom_pt + 1e-6:
                raise LayoutUnsatisfiable(
                    e.instance_id,
                    f"终检失败：第 {e.page_index+1} 页条目底 {bottom:.1f}pt > {page_bottom_pt:.1f}pt",
                )
    # 终检：跨栏目文字不得相撞（后栏标题文字顶 vs 前栏文字底）
    check_section_flow(plans, geometry=geometry)
    total_pages = max((e.page_index for s in plans for e in s.entries), default=0) + 1
    for s in plans:
        total_pages = max(total_pages, s.page_index + 1)
    return LayoutPlan(
        pages=total_pages,
        sections=plans,
        page_capacity_top_pt=page_top_pt,
        page_capacity_bottom_pt=page_bottom_pt,
        continuation_page_top_pt=geometry.continuation_page_top_pt,
    )


def section_ink_bottom_by_page(plans: list[SectionPlan], geometry: LayoutGeometry | None = None) -> list[dict[int, float]]:
    """每栏每页的**文字底**（最后一条 ink 底），用于跨栏目碰撞判定。"""
    geometry = geometry or LayoutGeometry()
    out: list[dict[int, float]] = []
    for plan in plans:
        per: dict[int, float] = {}
        for entry in plan.entries:
            offset = entry.text_top_offset_pt if entry.text_top_offset_pt is not None else geometry.text_top_pt
            bottom = entry.anchor_y_pt + offset + entry.text_height_pt
            per[entry.page_index] = max(per.get(entry.page_index, float("-inf")), bottom)
        out.append(per)
    return out


def check_section_flow(
    plans: list[SectionPlan], *, min_gap_pt: float = MIN_SECTION_GAP_PT,
    geometry: LayoutGeometry | None = None,
) -> None:
    """跨栏目流顺序校验：后一栏标题文字顶不得压到前一栏文字底。

    只在**同页**比较（跨页自然不相撞）。原件档案里栏目标题到上一栏文字底
    的可见间距最小为 11.85pt（campus→skills），因此 `min_gap_pt` 取 1pt 即可
    拦住真实碰撞，而不会把模板原有的紧凑间距误判为错误。
    """
    geometry = geometry or LayoutGeometry()
    ink = section_ink_bottom_by_page(plans, geometry)
    for i in range(1, len(plans)):
        nxt = plans[i]
        prev_ink = ink[i - 1].get(nxt.page_index)
        if prev_ink is None or prev_ink == float("-inf"):
            continue
        offset = nxt.title_text_top_offset_pt if nxt.title_text_top_offset_pt is not None else geometry.title_text_top_pt
        title_top = nxt.anchor_y_pt + offset
        if title_top < prev_ink + min_gap_pt - 1e-6:
            raise LayoutUnsatisfiable(
                nxt.section_id,
                f"栏目 {nxt.section_id} 标题文字顶 {title_top:.1f}pt 与上一栏文字底 "
                f"{prev_ink:.1f}pt 重叠"
                f"（间距 {title_top - prev_ink:.1f}pt < {min_gap_pt}pt）",
            )
