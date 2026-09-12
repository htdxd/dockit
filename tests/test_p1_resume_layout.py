# -*- coding: utf-8 -*-
"""P1 探针测试：简历组件化排版（resume_layout 包）。

分层（实施计划 §8：布局纯单测 + 环境标记端到端）：
- layout：确定性排版纯函数（顺序/绑定/翻页/UNSATISFIABLE）——无环境依赖
- emit：XML 克隆/组伸展/分页段落（lxml，无需 Word）
- e2e：Word COM 测量 + 渲染（标记 resume_e2e，无 Word 时 skip 并明确记录）

运行：
  uv run pytest tests/test_p1_resume_layout.py -q           # 纯单测
  uv run pytest tests/test_p1_resume_layout.py -q -k e2e    # 需 Windows+Word+pywin32
"""
from __future__ import annotations

import json
import zipfile
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


def test_emit_proto_snapshot_and_strip_title() -> None:
    """克隆条第 2+ 条必须剥掉标题框/图标/横线（复制条目不复制标题）。"""
    import copy as _copy
    from lxml import etree

    root = _root()
    proto = emit._anchor_by_docpr_name(root, "组合 216")
    node = proto
    while node is not None and etree.QName(node).localname != "AlternateContent":
        node = node.getparent()
    snap = _copy.deepcopy(node)
    anchor = emit.insert_proto(root, snap)
    clone = emit.clone_section_anchor(root, anchor, title_text="项目经历（Projects）")
    # strip 前有 4 wsp；strip 后只留正文框
    before = len(list(clone.iter(emit.WPS + "wsp")))
    removed = emit.strip_title_decorations(clone)
    after = len(list(clone.iter(emit.WPS + "wsp")))
    assert before == 4
    assert removed == 3
    assert after == 1
    # 剩余的是正文框（含 txbxContent 且可写文本）
    body = emit.find_body_wsp(clone)
    emit.set_box_text(body, "2020.01-2020.02           机构                           角色\n正文行")
    assert "机构" in emit._box_text(body)


def test_emit_clone_writes_title_text() -> None:
    import copy as _copy
    from lxml import etree

    root = _root()
    proto = emit._anchor_by_docpr_name(root, "组合 216")
    node = proto
    while node is not None and etree.QName(node).localname != "AlternateContent":
        node = node.getparent()
    anchor = emit.insert_proto(root, _copy.deepcopy(node))
    clone = emit.clone_section_anchor(root, anchor, title_text="项目经历（Projects）")
    title = emit.find_title_wsp(clone)
    assert title is not None
    assert emit._box_text(title) == "项目经历（Projects）"


def test_emit_full_chain_resize_updates_all_extents() -> None:
    """全链组伸展：wsp ext → wgp chExt/ext → anchor extent 全部同步。"""
    import copy as _copy
    from lxml import etree

    root = _root()
    proto = emit._anchor_by_docpr_name(root, "组合 216")
    node = proto
    while node is not None and etree.QName(node).localname != "AlternateContent":
        node = node.getparent()
    anchor = emit.insert_proto(root, _copy.deepcopy(node))
    clone = emit.clone_section_anchor(root, anchor, title_text="X（Y）")
    emit.strip_title_decorations(clone)
    body = emit.find_body_wsp(clone)
    emit.resize_group_child_bottom(clone, body, 200.0)
    # anchor extent ≥ 200+29.2
    aext = clone.find(emit.WP + "extent")
    assert int(aext.get("cy")) / 12700 >= 200.0 + 29.0
    # wgp ext 同步
    wgp = clone.find(".//{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}wgp")
    gxf = emit._xfrm_of_group(wgp)
    assert int(gxf.find(emit.A + "ext").get("cy")) / 12700 >= 200.0


def test_emit_output_opens_as_valid_docx(tmp_path: Path) -> None:
    """emit 产物 zip/XML 合法且 body 直接子级只有 p/sectPr。"""
    import copy as _copy
    from lxml import etree

    root = _root()
    # 快照全部栏目并重挂
    snaps = {}
    for sid, name in {
        "education": "组合 215", "internship": "组合 216", "campus": "组合 218",
        "skills": "组合 219", "summary": "组合 220",
    }.items():
        a = emit._anchor_by_docpr_name(root, name)
        node = a
        while node is not None and etree.QName(node).localname != "AlternateContent":
            node = node.getparent()
        snaps[sid] = _copy.deepcopy(node)
        # 移除原件栏目
        host_run = node.getparent()
        host_run.getparent().remove(host_run)
    proto = emit.insert_proto(root, snaps["internship"])
    clone = emit.clone_section_anchor(root, proto, title_text="教育背景（Education）")
    emit.set_box_text(emit.find_body_wsp(clone), "内容\n第二行")
    emit.resize_group_child_bottom(clone, emit.find_body_wsp(clone), 60.0)
    # 删除 proto 段
    p = proto.getparent()
    while etree.QName(p).localname != "p":
        p = p.getparent()
    p.getparent().remove(p)
    out = tmp_path / "out.docx"
    emit.save_document_xml(root, TEMPLATE, out)
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        xml = z.read("word/document.xml")
    r = etree.fromstring(xml)
    W = emit.W
    body = r.find(W + "body")
    kinds = {etree.QName(c).localname for c in body}
    assert kinds <= {"p", "sectPr"}, f"body 非法子级: {kinds}"
    # sectPr 必须是最后
    assert etree.QName(body[-1]).localname == "sectPr"


# ---------------- QA 反例（review Fix1/Fix2） ----------------

def _qa_stream(pages: dict[int, str]) -> dict[int, str]:
    return pages


def test_qa_ordered_completeness_passes_on_full_stream() -> None:
    from skill_toolbox.resume_layout.qa import check_content_completeness_ordered

    entries = [
        {"entry_id": "a", "text": "第一段内容\n第二段内容"},
        {"entry_id": "b", "text": "另起一段的内容"},
    ]
    issues = check_content_completeness_ordered(
        _qa_stream({1: "第一段内容 第二段内容 另起一段的内容"}), entries
    )
    assert issues == []


def test_qa_ordered_completeness_catches_truncation() -> None:
    """review 反例：追加渲染中不存在的文字必须失败（旧版只查前 12 字会漏）。"""
    from skill_toolbox.resume_layout.qa import check_content_completeness_ordered

    entries = [{"entry_id": "a", "text": "正常内容\n尾部新增不存在的字XYZQWE"}]
    issues = check_content_completeness_ordered(
        _qa_stream({1: "正常内容"}), entries
    )
    assert any(i.severity == "error" and "不完整" in i.detail for i in issues)


def test_qa_ordered_completeness_catches_duplication() -> None:
    """重复条目：产物只渲染一份时，第二条（游标后）必须匹配失败。"""
    from skill_toolbox.resume_layout.qa import check_content_completeness_ordered

    entries = [
        {"entry_id": "a", "text": "重复内容"},
        {"entry_id": "a-dup", "text": "重复内容"},
    ]
    issues = check_content_completeness_ordered(
        _qa_stream({1: "重复内容"}), entries
    )
    assert any(i.severity == "error" for i in issues)


def test_qa_ordered_completeness_catches_reordering() -> None:
    from skill_toolbox.resume_layout.qa import check_content_completeness_ordered

    entries = [
        {"entry_id": "a", "text": "条目甲"},
        {"entry_id": "b", "text": "条目乙"},
    ]
    issues = check_content_completeness_ordered(
        _qa_stream({1: "条目乙条目甲"}), entries
    )
    assert any("顺序错乱" in i.detail for i in issues)


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


def test_qa_extra_duplication_in_output_detected() -> None:
    """R2 反例：预期 A→B、产物 A→A→B 必须失败（额外重复内容检查）。"""
    from skill_toolbox.resume_layout.qa import check_no_extra_rendered_content

    pages = {1: "ALPHA内容 ALPHA内容 BETA内容"}
    entries = [
        {"entry_id": "a", "text": "ALPHA内容"},
        {"entry_id": "b", "text": "BETA内容"},
    ]
    issues = check_no_extra_rendered_content(pages, entries, allowed_extra_needles=[])
    errs = [i for i in issues if i.severity == "error"]
    assert any("额外正文" in i.detail for i in errs)
    assert any("出现次数不符" in i.detail and "多复制" in i.detail for i in errs)


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


def test_replay_uses_verbatim_xml_texts() -> None:
    """R2 Fix1：重放内容 = document.xml 原文（含自我评价 87 字 / 2 行版本）。"""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from skill_toolbox.resume_layout.generate import _original_section_texts

    orig = _original_section_texts()
    # 自我评价 87 字单段（manifest 是 106 字旧版——不得混入）
    assert len(orig["summary"]) == 1
    assert len(orig["summary"][0]) == 87
    assert "管控工作" in orig["summary"][0]      # XML 版特有表述
    assert "项目经验" not in orig["summary"][0]  # manifest 版表述（不得出现）
    # 实习栏 7 段（含中间空段）
    assert len(orig["internship"]) == 7
    assert orig["internship"][3] == ""


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
    import copy as _copy

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


# ---------------- e2e（Word COM，环境标记） ----------------

e2e = pytest.mark.e2e


@e2e
def test_e2e_measure_probe_line_pitch() -> None:
    """Word COM 探针：模板宿主中 3 行文本 pitch=18.0、行数正确。"""
    pytest.importorskip("pythoncom")
    import shutil

    from skill_toolbox.resume_layout.measure import WordMeasureProbe

    host = Path(__file__).parent / "_t109_measure_host.docx"
    shutil.copy(TEMPLATE, host)
    try:
        probe = WordMeasureProbe(host)
        rs = probe.measure_entries(3, 2, [
            {"id": "x", "text": "一\r二\r三\r四\r五\r六"},
        ])
        r = rs[0]
        assert r.wrapped_lines == 6
        assert r.line_pitch_pt == pytest.approx(18.0, abs=0.05)
        assert r.text_height_pt == pytest.approx(108.0, abs=0.5)
    finally:
        host.unlink(missing_ok=True)


@e2e
def test_e2e_generate_scenario_replay(tmp_path: Path) -> None:
    """端到端：replay 生成 + 渲染页数一致 + **QA 报告必须通过**（含测量门）。"""
    pytest.importorskip("pythoncom")
    import subprocess
    import sys

    gen = (
        PROJECT_ROOT
        / "backend/skill_toolbox/resume_layout/generate.py"
    )
    report_cli = (
        PROJECT_ROOT
        / "backend/skill_toolbox/resume_layout/report.py"
    )
    out = tmp_path / "ev"
    rc = subprocess.run(
        [sys.executable, str(gen), str(out), "replay"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    assert rc.returncode == 0, rc.stderr[-500:]
    result = json.loads(rc.stdout)
    assert result["ok"] is True
    assert result["rendered_pages"] == result["plan_pages"]
    assert (out / "replay" / "resume_replay.docx").is_file()
    # QA 汇总（review Fix2：e2e 必须检查 QA，不能只看生成成功）
    rc2 = subprocess.run(
        [sys.executable, str(report_cli), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    assert rc2.returncode == 0, rc2.stderr[-500:]
    summary = json.loads(rc2.stdout)
    assert summary["replay"]["passed"] is True, summary["replay"]["errors"]
