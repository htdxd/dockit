"""纯布局边界、测量失败门和段落样式回归；PDF 内容检查见 test_resume_component_qa。"""
from __future__ import annotations

from pathlib import Path

import pytest
from skill_toolbox.resume_layout import emit
from skill_toolbox.resume_layout.layout import (
    LayoutUnsatisfiable,
    MeasureResult,
    entry_body_height,
    plan_layout,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/templates/t109/template.docx"
)
PAGE_TOP = 193.35
PAGE_BOTTOM = 833.6
CAPACITY = PAGE_BOTTOM - PAGE_TOP


def _m(eid: str, lines: int) -> MeasureResult:
    return MeasureResult(eid, lines, lines * 18.0)


# ---------------- layout 纯函数 ----------------

def test_layout_single_section_positions() -> None:
    ms = {"e1": _m("e1", 4)}
    plan = plan_layout([{"id": "s", "title": "T", "entries": [{"id": "e1"}]}], ms)
    sec = plan.sections[0]
    assert sec.anchor_y_pt == PAGE_TOP
    ent = sec.entries[0]
    assert ent.anchor_y_pt == PAGE_TOP + 29.2
    assert ent.body_h_pt == entry_body_height(ms["e1"])
    assert plan.pages == 1


def test_layout_sequential_gap_and_push() -> None:
    """第二栏 y = 第一栏条目底 + 15.45 间距。"""
    ms = {"e1": _m("e1", 4), "e2": _m("e2", 2)}
    plan = plan_layout(
        [
            {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
            {"id": "b", "title": "B", "entries": [{"id": "e2"}]},
        ],
        ms,
    )
    b = plan.sections[1]
    first_bottom = PAGE_TOP + 29.2 + entry_body_height(ms["e1"])
    assert b.anchor_y_pt == pytest.approx(first_bottom + 15.45, abs=0.01)


def test_layout_title_binds_first_entry_to_next_page() -> None:
    """标题+首条放不下 → 整栏移下一页，标题不孤悬。"""
    # big 底 = 222.55+504.45+... ；使第二栏标题+首条超页底（footprint 语义：
    # 首条占用 = 29.2 偏移 + body_h，从上一栏条目底 + 15.45 起）
    ms = {"big": _m("big", 28), "e2": _m("e2", 4)}
    plan = plan_layout(
        [
            {"id": "a", "title": "A", "entries": [{"id": "big"}]},
            {"id": "b", "title": "B", "entries": [{"id": "e2"}]},
        ],
        ms,
    )
    b = plan.sections[1]
    assert b.page_index == 1, "标题+首条放不下应整体去第 2 页"
    assert b.anchor_y_pt == PAGE_TOP
    assert plan.pages == 2


def test_layout_overflow_entry_to_page2_without_title() -> None:
    """后续条目跨页续排（不重复标题、不带标题绑定）。"""
    ms = {"e1": _m("e1", 30), "e2": _m("e2", 4)}
    plan = plan_layout(
        [{"id": "a", "title": "A", "entries": [{"id": "e1"}, {"id": "e2"}]}], ms
    )
    entries = plan.sections[0].entries
    assert entries[0].page_index == 0
    assert entries[1].page_index == 1
    assert entries[1].anchor_y_pt == PAGE_TOP


def test_layout_unsatisfiable_single_entry_over_page() -> None:
    ms = {"huge": _m("huge", 50)}
    with pytest.raises(LayoutUnsatisfiable) as exc:
        plan_layout([{"id": "a", "title": "A", "entries": [{"id": "huge"}]}], ms)
    assert "huge" in str(exc)


def test_layout_rejects_entry_fitting_only_by_page_flip() -> None:
    """review 反例：620pt 文字高度的首条（占用 657.7pt > 容量 640.25pt）
    必须拒绝——即使翻页也放不下（翻页后仍需完整矩形校验）。"""
    ms = {"big": MeasureResult("big", 35, 620.0)}
    with pytest.raises(LayoutUnsatisfiable):
        plan_layout([{"id": "a", "title": "A", "entries": [{"id": "big"}]}], ms)
    # 前面有其他栏目时同样拒绝（不能靠翻页"放下"越界组件）
    ms2 = {"e1": _m("e1", 3), "big": MeasureResult("big", 35, 620.0)}
    with pytest.raises(LayoutUnsatisfiable):
        plan_layout(
            [
                {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
                {"id": "b", "title": "B", "entries": [{"id": "big"}]},
            ],
            ms2,
        )


def test_layout_critical_capacity_boundary() -> None:
    """临界容量：恰好放下 → bottom == 页底；超出 0.1pt → 拒绝。"""
    body_h = (833.6 - 193.35) - 29.2          # 首条恰好占满内容区
    plan = plan_layout(
        [{"id": "s", "title": "T", "entries": [{"id": "e"}]}],
        {"e": MeasureResult("e", 33, body_h - 8.45)},
    )
    e = plan.sections[0].entries[0]
    assert e.anchor_y_pt + e.body_h_pt == pytest.approx(833.6, abs=0.01)
    with pytest.raises(LayoutUnsatisfiable):
        plan_layout(
            [{"id": "s", "title": "T", "entries": [{"id": "e"}]}],
            {"e": MeasureResult("e", 33, body_h - 8.45 + 0.1)},
        )


def test_layout_k1_flip_still_validates_rect() -> None:
    """k>0 条目翻页后同样校验：正文高超过整页容量必须拒绝。"""
    ms = {"e1": _m("e1", 30), "big": MeasureResult("big", 40, 700.0)}
    with pytest.raises(LayoutUnsatisfiable):
        plan_layout([{"id": "a", "title": "A", "entries": [{"id": "e1"}, {"id": "big"}]}], ms)


def test_layout_missing_measurement_rejected() -> None:
    """缺真实测量 → 拒绝排布（禁止估算值兜底）。"""
    with pytest.raises(LayoutUnsatisfiable):
        plan_layout(
            [{"id": "a", "title": "A", "entries": [{"id": "ghost"}]}], {}
        )


def test_layout_deterministic_replay() -> None:
    """同输入同输出（重复生成语义一致）。"""
    ms = {"e1": _m("e1", 3), "e2": _m("e2", 5)}
    secs = [
        {"id": "a", "title": "A", "entries": [{"id": "e1"}]},
        {"id": "b", "title": "B", "entries": [{"id": "e2"}]},
    ]
    d1 = plan_layout(secs, ms).to_dict()
    d2 = plan_layout(secs, ms).to_dict()
    assert d1 == d2


# ---------------- emit XML（无需 Word） ----------------

def _root():
    return emit.load_document_xml(TEMPLATE)


# ---------------- QA 反例（review Fix1/Fix2） ----------------


def test_qa_measurement_gate_rejects_tampered_measurements() -> None:
    """review 反例：篡改测量（lines=999 / height=17982）必须使 QA 失败。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    measured = [{"entry_id": "e1", "wrapped_lines": 999, "text_height_pt": 17982.0}]
    rendered = [{"entry_id": "e1", "wrapped_lines": 4, "occupied_height_pt": 72.0}]
    issues = check_measurement_vs_render(measured, rendered)
    errs = [i for i in issues if i.severity == "error"]
    assert any("行数不符" in i.detail for i in errs)
    assert any("占用高度不符" in i.detail for i in errs)


def test_qa_measurement_gate_passes_only_within_tolerance() -> None:
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    ok = check_measurement_vs_render(
        [{"entry_id": "e1", "wrapped_lines": 4, "text_height_pt": 72.0}],
        [{"entry_id": "e1", "wrapped_lines": 4, "occupied_height_pt": 72.6}],
    )
    assert ok == []
    one_line_off = check_measurement_vs_render(
        [{"entry_id": "e1", "wrapped_lines": 5, "text_height_pt": 90.0}],
        [{"entry_id": "e1", "wrapped_lines": 4, "occupied_height_pt": 72.0}],
    )
    assert any(i.severity == "error" for i in one_line_off)


def test_qa_missing_render_counterpart_fails() -> None:
    """渲染对照缺失（未独立核验）→ error，不能默认通过。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    issues = check_measurement_vs_render(
        [{"entry_id": "e1", "wrapped_lines": 4, "text_height_pt": 72.0}], []
    )
    assert any(i.severity == "error" for i in issues)


# ---------------- QA 反例（review R2 Fix2/3/4） ----------------

def test_qa_empty_measurements_fail_id_set_gate() -> None:
    """R2 反例：空测量表 {} 传入必须失败（三方 ID 集合校验）。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    issues = check_measurement_vs_render([], [], expected_entry_ids=["edu-1", "intern-1"])
    errs = [i for i in issues if i.severity == "error"]
    assert any("缺少测量结果" in i.detail for i in errs)
    assert any("缺少渲染对照" in i.detail for i in errs)


def test_qa_abnormal_render_pitch_fails_pitch_gate() -> None:
    """R2 反例：两行起点相隔 21pt（行距异常）必须失败——行距门用真实行位置。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    rendered = [{"entry_id": "e1", "wrapped_lines": 2,
                 "occupied_height_pt": 39.0, "render_pitch_pt": 21.0}]
    measured = [{"entry_id": "e1", "wrapped_lines": 2, "text_height_pt": 36.0}]
    issues = check_measurement_vs_render(measured, rendered, expected_entry_ids=["e1"])
    assert any("行距异常" in i.detail for i in issues)


def test_qa_normal_pitch_passes() -> None:
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    ok = check_measurement_vs_render(
        [{"entry_id": "e1", "wrapped_lines": 4, "text_height_pt": 72.0}],
        [{"entry_id": "e1", "wrapped_lines": 4, "occupied_height_pt": 72.2,
          "render_pitch_pt": 18.05}],
        expected_entry_ids=["e1"],
    )
    assert ok == []


def test_qa_duplicate_entry_ids_rejected() -> None:
    """测量表 entry_id 重复必须失败（唯一性校验）。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    measured = [
        {"entry_id": "e1", "wrapped_lines": 4, "text_height_pt": 72.0},
        {"entry_id": "e1", "wrapped_lines": 4, "text_height_pt": 72.0},
    ]
    rendered = [{"entry_id": "e1", "wrapped_lines": 4, "occupied_height_pt": 72.0,
                 "render_pitch_pt": 18.0}]
    issues = check_measurement_vs_render(measured, rendered, expected_entry_ids=["e1"])
    assert any("重复" in i.detail for i in issues)


# ---------------- QA 反例（review R3 Fix1/Fix2） ----------------

def test_qa_render_error_result_fails() -> None:
    """R3 反例：渲染结果 {"error": "NOT FOUND"} 时 ID 集合一致也必须失败。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    measured = [{"entry_id": "e", "wrapped_lines": 4, "text_height_pt": 72.0}]
    rendered = [{"entry_id": "e", "error": "NOT FOUND"}]
    issues = check_measurement_vs_render(measured, rendered, expected_entry_ids=["e"])
    assert any("渲染提取失败" in i.detail for i in issues)


def test_qa_render_missing_numeric_field_fails() -> None:
    """R3 反例：渲染对照缺必需数值字段（无法核验）必须失败。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    measured = [{"entry_id": "e", "wrapped_lines": 4, "text_height_pt": 72.0}]
    rendered = [{"entry_id": "e", "wrapped_lines": 4}]  # 无 occupied_height_pt
    issues = check_measurement_vs_render(measured, rendered, expected_entry_ids=["e"])
    assert any("缺必需数值字段" in i.detail for i in issues)


def test_qa_invalid_numeric_values_fail() -> None:
    """渲染对照数值为 None/NaN/字符串时必须失败。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    measured = [{"entry_id": "e", "wrapped_lines": 4, "text_height_pt": 72.0}]
    rendered = [{"entry_id": "e", "wrapped_lines": None, "occupied_height_pt": "72"}]
    issues = check_measurement_vs_render(measured, rendered, expected_entry_ids=["e"])
    assert any(i.severity == "error" for i in issues)


def test_qa_cumulative_height_drift_capped() -> None:
    """R3 反例：20 行/379pt/19pt 行距的累计误差必须失败（容差上限 4pt）。

    19pt 行距在 18±1.0 容差边界内（行距门负责更大偏差，如 21pt），
    但 19pt×20 行的累计 +19pt 高度差必须被高度门拦截。
    """
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    measured = [{"entry_id": "e", "wrapped_lines": 20, "text_height_pt": 360.0}]
    rendered = [{"entry_id": "e", "wrapped_lines": 20,
                 "occupied_height_pt": 379.0, "render_pitch_pt": 19.0}]
    issues = check_measurement_vs_render(measured, rendered, expected_entry_ids=["e"])
    errs = [i.detail for i in issues if i.severity == "error"]
    assert any("占用高度不符" in d and "容差 4.0pt" in d for d in errs)


def test_qa_pitch_gate_boundary() -> None:
    """行距门边界：18±1.0 内通过、21pt 拦截。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    ok = check_measurement_vs_render(
        [{"entry_id": "e", "wrapped_lines": 3, "text_height_pt": 54.0}],
        [{"entry_id": "e", "wrapped_lines": 3, "occupied_height_pt": 55.0,
          "render_pitch_pt": 19.0}],  # 19.0-18.0 = 1.0 不越界（>1.0 才拦）
        expected_entry_ids=["e"],
    )
    assert ok == []
    bad = check_measurement_vs_render(
        [{"entry_id": "e", "wrapped_lines": 3, "text_height_pt": 54.0}],
        [{"entry_id": "e", "wrapped_lines": 3, "occupied_height_pt": 58.0,
          "render_pitch_pt": 21.0}],
        expected_entry_ids=["e"],
    )
    assert any("行距异常" in i.detail for i in bad)


def test_qa_tight_tolerance_passes_normal_variance() -> None:
    """正常渲染噪声（≤0.25pt/行）在收紧容差内通过。"""
    from skill_toolbox.resume_layout.qa import check_measurement_vs_render

    ok = check_measurement_vs_render(
        [{"entry_id": "e", "wrapped_lines": 6, "text_height_pt": 108.0}],
        [{"entry_id": "e", "wrapped_lines": 6, "occupied_height_pt": 108.18,
          "render_pitch_pt": 18.03}],
        expected_entry_ids=["e"],
    )
    assert ok == []


def test_style_snapshots_skip_empty_separator_paras() -> None:
    """R3 根修验证：样式快照跳过空段（实习 p3 的 sz=6 分隔段不进快照）。"""

    root = emit.load_document_xml(TEMPLATE)
    proto = emit._anchor_by_docpr_name(root, "组合 216")
    body = emit.find_body_wsp(proto)
    snaps = emit._para_style_snapshots(body.find(".//" + emit.W + "txbxContent"))
    # 原件 7 段（含 1 空段）→ 快照 6 条，且无 sz=6 的小字号样式
    assert len(snaps) == 6
    for s in snaps:
        if s["rPr"] is not None:
            sz = s["rPr"].find(emit.W + "sz")
            assert sz is None or int(sz.get(emit.W + "val")) >= 18  # ≥9pt
