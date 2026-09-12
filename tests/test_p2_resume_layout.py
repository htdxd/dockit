# -*- coding: utf-8 -*-
"""P2 测试：段落角色化样式映射 + 原件间距档案（P2-1）。

覆盖：
- 角色分桶（按 numPr 结构，不按段落序号）——R3 review 的 P2 缺陷回归门：
  grow 新增职责段必须仍是「职责段样式」（有 numPr、非粗体），不得继承第 2 条
  经历的标题样式；
- 间距档案：COM 逐段数据 + XML 外框 → 三层几何（文字边界/组件外框/栏目间距）
  的纯函数构建、条目间距识别（空段分隔）、未登记对的回落；
- 布局消费：栏目推进量取档案增量、条目间距取档案值、可见间距兜底；
- e2e（Word COM）：档案复现原件 anchor 位置、原内容重放页数与内容完整性、
  grow 新增职责段的 DOCX 事实。

运行：
  uv run pytest tests/test_p2_resume_layout.py -q            # 纯单测
  uv run pytest tests/test_p2_resume_layout.py -q -k e2e     # 需 Windows+Word+pywin32
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from skill_toolbox.resume_layout import emit, layout, spacing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/templates/t109/template.docx"
)
GEN = PROJECT_ROOT / "backend/skill_toolbox/resume_layout/generate.py"
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

def _probe_fixture() -> dict:
    """合成探针数据：两栏 + 实习栏内两条条目（空段分隔）。"""
    return {
        "frames": {
            "education": {"frame_top_pt": 100.0, "body_wsp_off_y_pt": 29.2, "body_h_pt": 80.0},
            "internship": {"frame_top_pt": 220.0, "body_wsp_off_y_pt": 29.2, "body_h_pt": 157.2},
        },
        "paragraphs": {
            "education": [
                {"top_pt": 132.2, "lines": 1, "text": "2005.07-2009.06 某大学"},
                {"top_pt": 150.2, "lines": 2, "text": "主修课程："},
            ],
            "internship": [
                {"top_pt": 252.2, "lines": 1, "text": "2012.06-至今 某公司"},
                {"top_pt": 270.2, "lines": 2, "text": "职责一"},
                {"top_pt": 306.2, "lines": 1, "text": ""},
                {"top_pt": 311.4, "lines": 1, "text": "2010.03-2012.03 另一公司"},
                {"top_pt": 329.4, "lines": 1, "text": "职责二"},
            ],
        },
        "order": ["education", "internship"],
    }


def test_archive_three_layers_and_pairs() -> None:
    arch = spacing.build_archive_from_probe(
        _probe_fixture(), template_id="t109", template_sha256="deadbeef"
    )
    edu = arch.sections["education"]
    assert edu.frame_top_pt == 100.0
    assert edu.frame_bottom_pt == pytest.approx(100.0 + 29.2 + 80.0)
    assert edu.text_ink_top_pt == 132.2
    assert edu.text_ink_bottom_pt == pytest.approx(150.2 + 36.0)
    assert edu.bottom_slack_pt == pytest.approx(209.2 - 186.2)
    assert edu.entry_gap_pt is None and edu.entry_count == 1

    intern = arch.sections["internship"]
    assert intern.entry_count == 2
    # 条目间距 = 下条文字顶 − 上条文字底 = 311.4 − (270.2 + 36)
    assert intern.entry_gap_pt == pytest.approx(5.2)

    pair = arch.pairs[("education", "internship")]
    assert pair.anchor_gap_pt == pytest.approx(220.0 - 209.2)
    assert pair.anchor_delta_from_prev_text_pt == pytest.approx(220.0 - 186.2)
    assert pair.visible_gap_pt == pytest.approx(
        220.0 + spacing.TITLE_TEXT_TOP_OFF_PT - 186.2
    )
    assert arch.delta_after("education", "unknown", 20.05) == 20.05
    assert arch.entry_gap_for("education", 8.45) == 8.45
    assert arch.entry_gap_for("internship", 8.45) == pytest.approx(5.2)


def test_archive_roundtrip_json(tmp_path: Path) -> None:
    arch = spacing.build_archive_from_probe(
        _probe_fixture(), template_id="t109", template_sha256="deadbeef"
    )
    out = tmp_path / "arch.json"
    spacing.save_archive(arch, out)
    back = spacing.load_archive(out)
    assert back.template_sha256 == "deadbeef"
    assert back.sections["internship"].entry_gap_pt == pytest.approx(5.2)
    assert back.pairs[("education", "internship")].anchor_gap_pt == pytest.approx(10.8)
    assert back.delta_after("education", "internship", 20.05) == pytest.approx(33.8)


def test_archive_frame_geometry_from_template_xml() -> None:
    """无 COM 的一半：外框几何直接来自原件 document.xml（原件只读）。"""
    frames = spacing._frame_geometry(TEMPLATE)
    assert frames["education"]["frame_top_pt"] == pytest.approx(193.35, abs=0.01)
    assert frames["internship"]["frame_top_pt"] == pytest.approx(318.0, abs=0.01)
    assert frames["campus"]["frame_top_pt"] == pytest.approx(519.85, abs=0.01)
    assert frames["skills"]["frame_top_pt"] == pytest.approx(644.5, abs=0.01)
    assert frames["summary"]["frame_top_pt"] == pytest.approx(751.15, abs=0.01)
    # summary 正文框（不是标题框）：高 43.95
    assert frames["summary"]["body_h_pt"] == pytest.approx(43.95, abs=0.05)


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


# ---------------- e2e（Word COM） ----------------

e2e = pytest.mark.e2e


@e2e
def test_e2e_archive_reproduces_original_anchor_positions(tmp_path: Path) -> None:
    """档案保真不变量：用档案自身的条目行数重放原件内容 →
    布局 anchor 必须复现原件 anchor（≤0.6pt），且仍是 1 页。"""
    pytest.importorskip("pythoncom")
    arch = spacing.ensure_archive(TEMPLATE, tmp_path / "spacing_archive.json")
    sections = []
    ms: dict[str, layout.MeasureResult] = {}
    for sid, sec in arch.sections.items():
        entries = []
        for i, lines in enumerate(sec.entry_lines):
            eid = f"{sid}-{i + 1}"
            entries.append({"id": eid})
            ms[eid] = _m(eid, lines)
        sections.append({"id": sid, "title": sid, "entries": entries})
    plan = layout.plan_layout(sections, ms, spacing=arch)
    assert plan.pages == 1
    for s in plan.sections:
        want = arch.sections[s.section_id].frame_top_pt
        assert s.anchor_y_pt == pytest.approx(want, abs=0.6), (
            f"{s.section_id}: {s.anchor_y_pt:.2f} vs 原件 {want:.2f}"
        )


@e2e
def test_e2e_grow_duty_paragraph_style_and_qa(tmp_path: Path) -> None:
    """端到端：grow 场景新增职责段在产物 DOCX 中必须有 numPr、不得加粗；
    机械 QA 仍须通过（测量门不被样式改动绕过）。"""
    pytest.importorskip("pythoncom")
    out = tmp_path / "ev"
    rc = subprocess.run(
        [sys.executable, str(GEN), str(out), "grow"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    assert rc.returncode == 0, rc.stderr[-500:]
    docx = out / "grow" / "resume_grow.docx"
    assert docx.is_file()
    root = None
    with zipfile.ZipFile(docx) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
    target = None
    for wsp in root.iter(WPS + "wsp"):
        tx = wsp.find(".//" + W + "txbxContent")
        if tx is None:
            continue
        if "季度复盘" in "".join(t.text or "" for t in tx.iter(W + "t")):
            target = tx
            break
    assert target is not None, "产物中找不到新增职责段"
    facts = [_facts(p) for p in _paras_of(target)]
    new_duty = [f for f in facts if "季度复盘" in f["text"]]
    assert new_duty, "新增职责段丢失"
    assert new_duty[0]["numpr"] is True, "新增职责段丢项目符号"
    assert new_duty[0]["bold"] is False, "新增职责段被加粗"
    header = [f for f in facts if f["text"].startswith("2012.06")]
    assert header and header[0]["bold"] is True, "条目标题段不应丢失标题样式"

    # QA（含测量门）必须通过
    rc2 = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "backend/skill_toolbox/resume_layout/report.py"),
         str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    assert rc2.returncode == 0, rc2.stderr[-500:]
    summary = json.loads(rc2.stdout)
    assert summary["grow"]["passed"] is True, summary["grow"]["errors"]


# ---------------- P2-4：正文框结构识别（agent 闭环暴露的重叠根因） ----------------

def test_body_box_identified_when_title_has_no_parentheses() -> None:
    """回归：标题不含全角括号英文时，正文框仍必须被正确识别。

    P2-4 真实 agent 闭环中，模型把栏目标题写成「教育背景」（不含
    「（Education）」）→ 旧的文本启发式识别失败，把**标题框**当成正文框，
    正文写进标题框、原件正文残留 → 渲染出真实文字重叠（QA 如实拦截）。
    修复后按结构识别：标题框 = 组合顶部（off.y≈0、高≈30.2）。
    """
    import copy as _copy

    root = emit.load_document_xml(TEMPLATE)
    proto = emit._anchor_by_docpr_name(root, "组合 215")
    node = proto
    while node is not None and etree.QName(node).localname != "AlternateContent":
        node = node.getparent()
    anchor = emit.insert_proto(root, _copy.deepcopy(node))

    clone = emit.clone_section_anchor(root, anchor, title_text="教育背景")
    title = emit.find_title_wsp(clone)
    body = emit.find_body_wsp(clone)
    assert title is not None and body is not None and title is not body
    assert emit._box_text(title) == "教育背景"
    # 正文框是原件大框（3 段），不能被换成标题框（1 段）
    assert len(list(body.find(".//" + emit.W + "txbxContent").iter(emit.W + "p"))) == 3

    emit.set_box_text(
        body,
        "2005.07-2009.06            北京某大学             市场营销（本科）\n"
        "主修课程：管理学、微观经济学。",
        style_roles=emit.build_style_roles(
            body.find(".//" + emit.W + "txbxContent")
        ),
    )
    text = emit._box_text(body)
    assert "2005.07-2009.06" in text and "主修课程" in text
    assert emit._box_text(title) == "教育背景", "标题框不得被正文覆盖"
    # 原件正文不得残留（残留会在渲染上表现为文字重叠）
    assert "简历模板资源网师范大学" not in text


def test_title_box_recognition_is_geometric_not_textual() -> None:
    """标题框识别只看几何（顶部 + 高度），与文本内容无关。"""
    root = emit.load_document_xml(TEMPLATE)
    for name in ("组合 215", "组合 216", "组合 218", "组合 219", "组合 220"):
        anchor = emit._anchor_by_docpr_name(root, name)
        title = emit.find_title_wsp(anchor)
        body = emit.find_body_wsp(anchor)
        assert title is not None, f"{name} 未识别出标题框"
        assert body is not None and body is not title
        assert emit._box_geom(title)[0] <= 1.0, "标题框必须在组合顶部"
        assert emit._box_geom(title)[1] >= emit.TITLE_BOX_MIN_H_PT
        assert emit._box_geom(body)[0] > emit._box_geom(title)[0], "正文框在标题框下方"


# ---------------- P2-R1：渲染侧条目边界定位（跨页/多条目回归） ----------------

def test_locate_line_top_tolerates_pdf_word_splitting() -> None:
    """PDF 会按字体内部分词，单一大前缀匹配会导致条目边界丢失。

    P2-R1 回放中 `skills` 栏插入第 2 条后，第 1 条的 head 匹配失败 → 边界
    回退到「页内 band」，把第 2 条的行算进第 1 条（测量 3 vs 渲染 5）。
    """
    from skill_toolbox.resume_layout.report import locate_line_top

    page = [
        {"page": 0, "y0": 669.81, "y1": 683.65, "x0": 42.0, "x1": 120,
         "text": "普通话一级甲等；"},
        {"page": 0, "y0": 687.81, "y1": 701.65, "x0": 42.0, "x1": 300,
         "text": "大学英语四/六级（CET-4/6），良好的听说读写能力，快速浏览英语专业文件及书籍；"},
        {"page": 0, "y0": 705.81, "y1": 719.65, "x0": 42.0, "x1": 200,
         "text": "通过全国计算机二级考试，熟练运用"},
        {"page": 0, "y0": 732.20, "y1": 746.05, "x0": 42.0, "x1": 120,
         "text": "熟练使用"},
        {"page": 0, "y0": 732.20, "y1": 746.05, "x0": 89.3, "x1": 150,
         "text": "Python/SQL"},
        {"page": 0, "y0": 750.20, "y1": 764.05, "x0": 42.0, "x1": 260,
         "text": "具备产品原型设计与用户研究经验。"},
    ]
    # head 被 PDF 切成两个词：逐级缩短前缀后仍能定位（且必须落在第 2 条）
    assert locate_line_top(page, "熟练使用 Python/SQL 完成数据分析与报表自动化；") == 732.20
    # 带项目符号前缀的渲染词同样可定位
    bullets = [{"page": 0, "y0": 335.0, "y1": 348.8, "x0": 42.0, "x1": 300,
                "text": "⚫负责公司线上端资源的销售工作（以开拓客户为主）"}]
    assert locate_line_top(bullets, "负责公司线上端资源的销售工作（以开拓客户为主）；") == 335.0
    # 页面里没有的文字必须返回 None（不得误命中）
    assert locate_line_top(page, "完全不存在的一句话") is None
    # fitz tuple 形式（index 4 = 文本）兼容
    assert locate_line_top(
        [(42.0, 669.81, 120.0, 683.65, "普通话一级甲等；", 0, 0, 0)],
        "普通话一级甲等；",
    ) == 669.81


def test_entry_band_prefers_own_last_line_over_page_band() -> None:
    """下一条目首行无法定位时，边界必须回落到**本条末行**。

    否则页内 band 会把下一条目的行算进本条：P2-R1 回放中 skills 栏插入第 2 条
    后，第 1 条被算成 5 行（真实 3 行），QA 报「行数不符」把合法布局判失败。
    """
    from skill_toolbox.resume_layout.report import locate_entry_band

    page = [
        {"page": 0, "y0": 669.81, "y1": 683.65, "x0": 42.0, "x1": 120,
         "text": "普通话一级甲等；"},
        {"page": 0, "y0": 687.81, "y1": 701.65, "x0": 42.0, "x1": 300,
         "text": "大学英语四/六级（CET-4/6），良好的听说读写能力。"},
        {"page": 0, "y0": 705.81, "y1": 719.65, "x0": 42.0, "x1": 260,
         "text": "通过全国计算机二级考试，熟练运用 office 相关软件。"},
        # 第 2 条：首行在页面里是改写后的文字（模拟定位失败）
        {"page": 0, "y0": 732.20, "y1": 746.05, "x0": 42.0, "x1": 260,
         "text": "熟练使用 Python/SQL 完成数据分析与报表自动化；"},
        {"page": 0, "y0": 750.20, "y1": 764.05, "x0": 42.0, "x1": 260,
         "text": "具备产品原型设计与用户研究经验。"},
    ]
    band = locate_entry_band(
        page,
        head="普通话一级甲等；",
        tail="通过全国计算机二级考试，熟练运用 office 相关软件。",
        next_head="这条首行不在页面里（模拟定位失败）",
    )
    assert band["boundary"] == "own_last_line", band
    own = [w for w in page if band["y0"] - 0.5 <= w["y0"] < band["y_end"]]
    assert len(own) == 3, [w["text"] for w in own]
    assert band["y_end"] == pytest.approx(719.65 + 0.05)
    # 正常情况（下一条目首行可定位）优先用它，且不包含本条末行之后的文字
    band2 = locate_entry_band(
        page, head="普通话一级甲等；",
        tail="通过全国计算机二级考试，熟练运用 office 相关软件。",
        next_head="熟练使用 Python/SQL 完成数据分析与报表自动化；",
    )
    assert band2["boundary"] == "next_entry"
    assert band2["y_end"] == pytest.approx(732.20 - 0.05)
    # 首行定位不到 → NOT FOUND（不得静默给出错误的区间）
    assert locate_entry_band(page, "不存在的一句话", "也不存在") == {"error": "NOT FOUND"}


def test_paragraph_block_includes_wrapped_lines_but_not_next_entry() -> None:
    """末段边界必须包含本段**换行**行，且不含下一条目首行。

    反例（P2-R1）：summary 栏一段文字换 2 行、且它是本栏最后一条 → 若只取
    末段首行，会算成 1 行（测量 2 vs 渲染 1）。
    另一半：下一条目首行低 19.04pt（档案间距 5.2 + ink 13.84）时不能被并入。
    """
    from skill_toolbox.resume_layout.report import _paragraph_block_bottom

    wrapped = [
        {"page": 0, "y0": 784.40, "y1": 798.24, "x0": 42.0, "x1": 560,
         "text": "深度互联网从业人员，对互联网保持高度的敏感性和关注度，熟悉产品开发流程，"},
        {"page": 0, "y0": 802.40, "y1": 816.24, "x0": 42.0, "x1": 300,
         "text": "交互设计能力，能独立承担 APP 和 WEB 项目的管控工作。"},
    ]
    assert _paragraph_block_bottom(wrapped, 784.40) == pytest.approx(816.24)

    with_next_entry = wrapped + [
        # 下一条目首行：784.40 + 18.0×2 + 19.04 ≈ 839.44（> 18.6 的段内阈值）
        {"page": 0, "y0": 839.44, "y1": 853.28, "x0": 42.0, "x1": 200,
         "text": "下一条目首行"},
    ]
    assert _paragraph_block_bottom(with_next_entry, 784.40) == pytest.approx(816.24)
