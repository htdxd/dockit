"""段落角色样式与间距档案消费的纯函数回归。"""
from __future__ import annotations

from pathlib import Path

import pytest
from skill_toolbox.resume_layout import emit, layout

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/templates/t109/template.docx"
)
PAGE_TOP = 193.35
PAGE_BOTTOM = 833.6

W = emit.W


def _m(eid: str, lines: int) -> layout.MeasureResult:
    return layout.MeasureResult(eid, lines, lines * 18.0)


# ---------------- 角色化样式（P2-1 主修项） ----------------

def _paras_of(tx) -> list:
    return list(tx.iter(W + "p"))


def _facts(p) -> dict:
    ppr = p.find(W + "pPr")
    numpr = ppr.find(W + "numPr") if ppr is not None else None
    rpr = None
    for r in p.iter(W + "r"):
        rpr = r.find(W + "rPr")
        break
    return {
        "text": "".join(t.text or "" for t in p.iter(W + "t")),
        "numpr": numpr is not None,
        "bold": (rpr is not None and rpr.find(W + "b") is not None),
    }


def test_style_roles_bucket_by_numpr_structure() -> None:
    """实习栏原型：标题段 2 个（无 numPr、粗体），职责段 4 个（numPr）。"""
    root = emit.load_document_xml(TEMPLATE)
    anchor = emit._anchor_by_docpr_name(root, "组合 216")
    tx = emit.find_body_wsp(anchor).find(".//" + W + "txbxContent")
    roles = emit.build_style_roles(tx)
    assert len(roles[emit.ROLE_HEADER]) == 2
    assert len(roles[emit.ROLE_DUTY]) == 4
    for snap in roles[emit.ROLE_HEADER]:
        assert snap["has_numpr"] is False
        assert snap["rPr"] is not None and snap["rPr"].find(W + "b") is not None
    for snap in roles[emit.ROLE_DUTY]:
        assert snap["has_numpr"] is True


def test_new_duty_line_never_inherits_header_style() -> None:
    """R3 review 的 P2 缺陷回归门。

    原型 7 段（含 1 个 3pt 分隔空段）在第 2 条经历后有**第二个标题段**；
    位置式取样会把第 4 个内容行映射到该标题段 → 加粗且丢项目符号。
    角色化后第 4 行必须仍是职责段样式。
    """
    root = emit.load_document_xml(TEMPLATE)
    anchor = emit._anchor_by_docpr_name(root, "组合 216")
    body = emit.find_body_wsp(anchor)
    tx = body.find(".//" + W + "txbxContent")

    # 先固定缺陷来源：位置式取样的第 4 个内容行正是「第 2 条标题段」
    positional = emit._para_style_snapshots(tx)
    assert positional[3]["has_numpr"] is False, "位置式第 4 行 = 标题段（旧缺陷来源）"

    roles = emit.build_style_roles(tx)
    text = (
        "2012.06-至今               广州简历模板资源网信息科技有限公司           市场营销（实习生）\n"
        "负责公司线上端资源的销售工作（以开拓客户为主）；\n"
        "实时了解行业的变化，跟踪客户的详细数据；\n"
        "负责季度复盘与增长实验设计，通过 A/B 测试优化投放组合。"
    )
    emit.set_box_text(body, text, style_roles=roles)
    facts = [_facts(p) for p in _paras_of(tx)]
    assert len(facts) == 4
    assert facts[0]["numpr"] is False and facts[0]["bold"] is True
    for i, f in enumerate(facts[1:], start=1):
        assert f["numpr"] is True, f"第 {i} 行丢项目符号：{f['text'][:12]}"
        assert f["bold"] is False, f"第 {i} 行被加粗：{f['text'][:12]}"


def test_roles_skip_empty_separator_and_no_numpr_fallback() -> None:
    """分隔空段不进任何角色桶；无 numPr 的正文框按「首段=标题、其余=职责」分。"""
    root = emit.load_document_xml(TEMPLATE)
    internship_tx = emit.find_body_wsp(
        emit._anchor_by_docpr_name(root, "组合 216")
    ).find(".//" + W + "txbxContent")
    roles = emit.build_style_roles(internship_tx)
    all_snaps = roles[emit.ROLE_HEADER] + roles[emit.ROLE_DUTY]
    assert len(all_snaps) == 6, "7 段中 1 个分隔空段必须被跳过"
    for snap in all_snaps:
        rpr = snap["rPr"]
        sz = rpr.find(W + "sz") if rpr is not None else None
        assert sz is None or int(sz.get(W + "val")) >= 18, "小字号分隔段样式泄漏"

    # 教育栏无 numPr：标题=首段、职责=其余段
    edu_tx = emit.find_body_wsp(
        emit._anchor_by_docpr_name(root, "组合 215")
    ).find(".//" + W + "txbxContent")
    edu_roles = emit.build_style_roles(edu_tx)
    assert len(edu_roles[emit.ROLE_HEADER]) == 1
    assert len(edu_roles[emit.ROLE_DUTY]) == 2
    assert all(s["has_numpr"] is False for s in edu_roles[emit.ROLE_DUTY])


def test_no_header_detection_on_plain_text_entry() -> None:
    """无日期/无列对齐的首行（如技能栏条目）走职责角色，不被误判成标题。"""
    root = emit.load_document_xml(TEMPLATE)
    body = emit.find_body_wsp(emit._anchor_by_docpr_name(root, "组合 219"))
    tx = body.find(".//" + W + "txbxContent")
    roles = emit.build_style_roles(tx)
    emit.set_box_text(body, "普通话一级甲等；\n通过全国计算机二级考试。", style_roles=roles)
    facts = [_facts(p) for p in _paras_of(tx)]
    assert all(f["bold"] is False for f in facts)


def test_header_detection_helpers() -> None:
    assert emit.looks_like_entry_header("2012.06-至今               广州某某公司           实习生")
    assert emit.looks_like_entry_header("2005.07-2009.06  某某大学")
    assert emit.looks_like_entry_header("2017.09-2018.06           校学生会            部门骨干")
    assert not emit.looks_like_entry_header("负责公司线上端资源的销售工作（以开拓客户为主）")
    assert not emit.looks_like_entry_header("负责季度复盘与增长实验设计，通过 A/B 测试优化投放组合")


# ---------------- 间距档案（三层几何） ----------------


# ---------------- 布局消费档案 ----------------

class _FakeSpacing:
    def __init__(self, deltas=None, entry_gaps=None, default_delta=20.05) -> None:
        self.deltas = deltas or {}
        self.entry_gaps = entry_gaps or {}
        self.default_delta = default_delta

    def delta_after(self, prev_id, next_id, default):
        return self.deltas.get((prev_id, next_id), default)

    def entry_gap_for(self, section_id, default):
        return self.entry_gaps.get(section_id, default)


def test_layout_uses_archive_section_delta() -> None:
    """栏目 y = 上一栏文字底 + 档案增量（不再用固定框高推 y）。"""
    ms = {"e1": _m("e1", 4), "s2": _m("s2", 2)}
    fake = _FakeSpacing(deltas={("a", "b"): 10.0})
    plan = layout.plan_layout(
        [
            {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
            {"id": "b", "title": "B", "entries": [{"id": "s2"}]},
        ],
        ms,
        spacing=fake,
    )
    a = plan.sections[0]
    text_bottom = a.entries[0].anchor_y_pt + layout.BODY_FIRST_TEXT_OFF_PT + 72.0
    assert plan.sections[1].anchor_y_pt == pytest.approx(text_bottom + 10.0)
    assert a.gap_source == "archive"
    assert a.gap_after_pt == pytest.approx(10.0)
    # 未登记的有序对回落默认增量
    plan2 = layout.plan_layout(
        [
            {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
            {"id": "c", "title": "C", "entries": [{"id": "s2"}]},
        ],
        ms,
        spacing=fake,
    )
    assert plan2.sections[1].anchor_y_pt == pytest.approx(text_bottom + 20.05)
    assert plan2.sections[0].gap_source == "default"


def test_layout_uses_archive_entry_gap() -> None:
    """同栏条目间距取档案值：下条文字顶 = 上条文字底 + 5.2pt。"""
    ms = {"e1": _m("e1", 4), "e2": _m("e2", 2)}
    fake = _FakeSpacing(entry_gaps={"a": 5.2})
    plan = layout.plan_layout(
        [{"id": "a", "title": "A", "entries": [{"id": "e1"}, {"id": "e2"}]}],
        ms,
        spacing=fake,
    )
    e1, e2 = plan.sections[0].entries
    bottom1 = e1.anchor_y_pt + layout.BODY_FIRST_TEXT_OFF_PT + 72.0
    assert e2.anchor_y_pt + layout.BODY_FIRST_TEXT_OFF_PT == pytest.approx(bottom1 + 5.2)
    # 无档案时回落 P1 行为（首行偏移 + 框底余量 = 8.45）
    plan2 = layout.plan_layout(
        [{"id": "a", "title": "A", "entries": [{"id": "e1"}, {"id": "e2"}]}], ms
    )
    e1b, e2b = plan2.sections[0].entries
    bottom1b = e1b.anchor_y_pt + layout.BODY_FIRST_TEXT_OFF_PT + 72.0
    assert e2b.anchor_y_pt + layout.BODY_FIRST_TEXT_OFF_PT == pytest.approx(bottom1b + 8.45)


def test_layout_min_visible_gap_guard() -> None:
    """档案给出过小/负增量时，标题文字顶仍须让开上一栏文字底。"""
    ms = {"e1": _m("e1", 2)}
    fake = _FakeSpacing(deltas={("a", "b"): -50.0})
    plan = layout.plan_layout(
        [
            {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
            {"id": "b", "title": "B", "entries": [{"id": "e1"}]},
        ],
        ms,
        spacing=fake,
    )
    sec_a, sec_b = plan.sections
    text_bottom = sec_a.entries[0].anchor_y_pt + layout.BODY_FIRST_TEXT_OFF_PT + 36.0
    title_ink_top = sec_b.anchor_y_pt + layout.TITLE_TEXT_TOP_OFF_PT
    assert title_ink_top - text_bottom == pytest.approx(layout.MIN_VISIBLE_GAP_PT)
    assert sec_a.gap_source == "min_visible_gap"


def test_layout_growth_shifts_following_sections_by_growth() -> None:
    """内容变长 → 后续栏目同步下移（档案增量相对文字底，不是固定框高）。"""
    ms4 = {"e1": _m("e1", 4), "e2": _m("e2", 2)}
    ms5 = {"e1": _m("e1", 5), "e2": _m("e2", 2)}
    secs = [
        {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
        {"id": "b", "title": "B", "entries": [{"id": "e2"}]},
    ]
    fake = _FakeSpacing(deltas={("a", "b"): 19.45})
    p4 = layout.plan_layout(secs, ms4, spacing=fake)
    p5 = layout.plan_layout(secs, ms5, spacing=fake)
    assert p5.sections[1].anchor_y_pt - p4.sections[1].anchor_y_pt == pytest.approx(18.0)
    assert p5.sections[1].anchor_y_pt == pytest.approx(
        p4.sections[1].anchor_y_pt + 18.0
    )


def test_layout_archive_does_not_disable_overflow_gate() -> None:
    """档案不放松越界门：条目矩形越界仍必须失败。"""
    ms = {"e1": _m("e1", 40)}
    fake = _FakeSpacing(deltas={("a", "b"): 5.0})
    with pytest.raises(layout.LayoutUnsatisfiable):
        layout.plan_layout(
            [
                {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
                {"id": "b", "title": "B", "entries": [{"id": "e1"}]},
            ],
            ms,
            spacing=fake,
        )


# ---------------- P2-4：正文框结构识别（agent 闭环暴露的重叠根因） ----------------


# ---------------- P2-R1：渲染侧条目边界定位（跨页/多条目回归） ----------------
