"""真实材料生成暴露的排版与 QA 回归；无需 Word 或模型调用。"""
import hashlib
import io

import pytest
from lxml import etree
from PIL import Image

from skill_toolbox.resume_layout import emit, layout, pipeline, report
from skill_toolbox.resume_layout.t109 import TEMPLATE


@pytest.mark.parametrize("size", [(203, 204), (600, 900), (900, 600)])
def test_photo_keeps_rendered_aspect_inside_existing_group(size):
    root = emit.load_document_xml(TEMPLATE)
    anchor = emit._anchor_by_docpr_name(root, "组合 12")
    pic = anchor.find(".//{http://schemas.openxmlformats.org/drawingml/2006/picture}pic")
    xfrm = pic.find(".//" + emit.A + "xfrm")
    group = anchor.find(".//" + emit.WPG + "grpSpPr/" + emit.A + "xfrm")
    original_group = etree.tostring(group)
    original_extent = etree.tostring(anchor.find(emit.WP + "extent"))
    ext, off = xfrm.find(emit.A + "ext"), xfrm.find(emit.A + "off")
    center = [int(off.get(k)) + int(ext.get(c)) / 2 for k, c in (("x", "cx"), ("y", "cy"))]
    image = io.BytesIO()
    Image.new("RGB", size).save(image, format="PNG")
    emit.replace_photo(anchor, image.getvalue())
    assert etree.tostring(group) == original_group
    assert etree.tostring(anchor.find(emit.WP + "extent")) == original_extent
    outer, inner = group.find(emit.A + "ext"), group.find(emit.A + "chExt")
    displayed = [int(ext.get(axis)) * int(outer.get(axis)) / int(inner.get(axis))
                 for axis in ("cx", "cy")]
    assert displayed[0] / displayed[1] == pytest.approx(size[0] / size[1], abs=1e-5)
    for expected, (k, c) in zip(center, (("x", "cx"), ("y", "cy"))):
        assert int(off.get(k)) + int(ext.get(c)) / 2 == pytest.approx(expected, abs=1)


def test_measurement_receives_same_paragraph_bold_roles_as_emit(tmp_path, monkeypatch):
    captured = []

    def measure_hosts(entries):
        for entry in entries:
            root = emit.load_document_xml(entry["host"])
            body = emit.find_body_wsp(emit._anchor_by_docpr_name(root, "ResumeMeasureBody"))
            captured.append({
                "paragraph_bold": [
                    p.find("./" + emit.W + "r/" + emit.W + "rPr/" + emit.W + "b") is not None
                    for p in body.findall(".//" + emit.W + "txbxContent/" + emit.W + "p")
                ]
            })
        return []

    monkeypatch.setattr(pipeline.measure, "measure_documents", measure_hosts)
    scenario = {"sections": [
        {"id": "intent", "entries": [{"id": "intent-1", "text": "求职算法工程师"}]},
        {"id": "internship", "entries": [{
        "id": "intern-1",
        "text": "2025.07 - 2026.01    某公司    算法实习生\n复现 Nano-vLLM 推理引擎\n优化模型吞吐",
    }]}]}
    pipeline.measure_scenario(scenario, tmp_path, template=TEMPLATE)
    assert captured[0]["paragraph_bold"] == [False]
    assert captured[1]["paragraph_bold"] == [True, False, False]


def _word(text, y, x=42, page=0):
    return {"text": text, "y0": y, "y1": y + 13.8, "x0": x,
            "x1": x + 100, "page": page}


def test_custom_titles_and_shared_prefixes_are_counted_in_full():
    titles = ["技能特长（Skills）", "项目经历（Research）", "项目经历（Projects）"]
    words = [_word(t, 200 + 40 * i) for i, t in enumerate(titles)]
    assert not report.check_title_uniqueness(words, titles)
    issues = report.check_title_uniqueness(words + [_word(titles[0], 400)], titles)
    assert len(issues) == 1
    assert "2 次" in issues[0].detail


def test_same_header_prefix_does_not_select_previous_entry():
    words = [
        _word("公司项目", 555), _word("RAG 检索系统", 555, 160),
        _word("独立负责", 555, 420),
        _word("公司项目", 650), _word("Nano-vLLM 推理引擎", 650, 160),
        _word("独立负责", 650, 420),
    ]
    assert report.locate_line_top(words, "公司项目 RAG 检索系统 独立负责") == 555
    assert report.locate_line_top(words, "公司项目 Nano-vLLM 推理引擎 独立负责") == 650
    assert report.locate_line_top(words, "公司项目 完全不同的条目") is None


def test_internal_separator_word_is_preserved_but_leading_bullet_is_removed():
    words = [
        _word("⚫", 100, 30), _word("计算机专业", 100, 42),
        _word("·", 100, 130), _word("本科", 100, 145),
    ]
    assert report._rendered_lines(words) == [(0, 100, "计算机专业·本科")]
    assert report.locate_line_top(words, "计算机专业 · 本科") == 100


def test_identical_complete_headers_consume_distinct_rendered_entries(monkeypatch):
    words = [
        (42, 100, 150, 113.8, "某某大学", 0, 0, 0),
        (42, 118, 150, 131.8, "软件工程本科", 0, 1, 0),
        (42, 145, 150, 158.8, "某某大学", 0, 2, 0),
        (42, 163, 150, 176.8, "人工智能硕士", 0, 3, 0),
    ]

    class Page:
        def get_text(self, kind):
            if kind == "words":
                return words
            return {"blocks": [{"lines": [
                {"spans": [{"text": w[4], "bbox": w[:4], "origin": (w[0], w[1] + 11)}]}
                for w in words
            ]}]}

    monkeypatch.setattr(report.fitz, "open", lambda _: [Page()])
    scenario = {"sections": [{"id": "education", "entries": [
        {"id": "bachelor", "text": "某某大学\n软件工程本科"},
        {"id": "master", "text": "某某大学\n人工智能硕士"},
    ]}]}
    # 刻意不提供预计 y；定位必须只依据真实渲染文本与已消费区间。
    plan = {"sections": [{"section_id": "education", "entries": [
        {"instance_id": "bachelor", "page_index": 0},
        {"instance_id": "master", "page_index": 0},
    ]}]}
    rows = report.extract_rendered_entries("unused.pdf", scenario, plan)
    assert [r["first_line_top_pt"] for r in rows] == [100, 145]
    assert [r["wrapped_lines"] for r in rows] == [2, 2]
    assert [r["occupied_height_pt"] for r in rows] == pytest.approx([36, 36])


def test_wrapped_header_maps_to_first_rendered_line():
    words = [_word("公司项目 Nano-vLLM", 650), _word("推理引擎 独立负责", 668)]
    assert report.locate_line_top(words, "公司项目 Nano-vLLM 推理引擎 独立负责") == 650


def _cross_page_plan():
    sections = [
        {"id": "projects", "title": "项目经历", "entries": [{"id": "a"}, {"id": "b"}]},
        {"id": "skills", "title": "技能特长", "entries": [{"id": "c"}]},
    ]
    measured = {
        "a": layout.MeasureResult("a", 29, 522),
        "b": layout.MeasureResult("b", 5, 90),
        "c": layout.MeasureResult("c", 11, 198),
    }
    return sections, layout.plan_layout(sections, measured)


def test_next_section_uses_last_page_bottom_after_section_spans_pages():
    _, plan = _cross_page_plan()
    projects, skills = plan.sections
    assert [e.page_index for e in projects.entries] == [0, 1]
    assert plan.pages == 2
    assert skills.page_index == 1
    assert projects.gap_after_pt < 30


@pytest.mark.parametrize("parts", [
    ["学历：本科"],
    ["学", "历", "：", "本科"],
    ["学历：", "本", "科"],
])
def test_header_replacement_preserves_label_across_run_shapes(parts):
    anchor = etree.Element(emit.WPS + "wsp")
    tx = etree.SubElement(anchor, emit.W + "txbxContent")
    p = etree.SubElement(tx, emit.W + "p")
    for part in parts:
        r = etree.SubElement(p, emit.W + "r")
        etree.SubElement(r, emit.W + "t").text = part
    emit.set_info_fields(anchor, {"degree": "硕士"})
    assert "".join(p.itertext()) == "学历：硕士"
    emit.set_info_fields(anchor, {"degree": ""})
    assert "".join(p.itertext()) == "学历："


def test_each_page_has_master_decorations_and_unique_anchor_ids(tmp_path):
    before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    sections, plan = _cross_page_plan()
    for section in sections:
        for entry in section["entries"]:
            entry["text"] = "用于验证分页的正文"
    out = tmp_path / "two.docx"
    pipeline.emit_scenario({"sections": sections}, plan, out, template=TEMPLATE)
    root = emit.load_document_xml(out)
    page = 0
    decoration_counts = {}
    for paragraph in root.find(emit.W + "body"):
        page += sum(b.get(emit.W + "type") == "page" for b in paragraph.iter(emit.W + "br"))
        names = [d.get("name", "") for d in paragraph.iter(emit.WP + "docPr")]
        decoration_counts[page] = decoration_counts.get(page, 0) + sum(
            name.startswith(("矩形 213", "组合 3")) for name in names
        )
    assert decoration_counts == {0: 2, 1: 2}
    ids = [d.get("id") for d in root.iter(emit.WP + "docPr")]
    assert len(ids) == len(set(ids))
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == before
