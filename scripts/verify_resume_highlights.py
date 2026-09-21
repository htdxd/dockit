"""20 个固定亮点样例；默认只列样例，--execute 才调用本机 Word。

uv run --extra web scripts/verify_resume_highlights.py --execute
成功样例与输入拒绝反例分开计数；本脚本不代替双模型调用或人工视觉评审。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

TEMPLATES = ("t001", "t109")
SCENARIOS = (
    "short_project", "long_project", "long_link", "large_metrics", "inline_metrics",
    "body_phrase", "field_emphasis", "background", "local_edit", "undo",
)
NEGATIVES = ("missing_anchor", "edit_breaks_anchor")
PHRASE = "检索耗时降低 30%"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def case_content(name):
    """每次返回独立内容；所有数字、链接与表达均由固定输入提供。"""
    entry = {
        "id": "demo", "organization": "开源检索工具", "role": "个人项目",
        "tech_stack": "Python、TypeScript", "date": "2024.01–2024.06",
        "text": [f"实现缓存与索引优化，{PHRASE}。", "补充文档与回归测试。"],
        "details": [
            {"label": "Stars", "value": "120", "emphasis": "bold_accent"},
            {"label": "Forks", "value": "18", "emphasis": "bold"},
            {"label": "源码", "value": "example/search", "link": "https://github.com/example/search",
             "emphasis": "accent"},
        ],
        "source_ids": ["fixture"],
    }
    if name == "long_project":
        entry["organization"] = "面向中文知识库的多模态资料检索与问答管理平台"
    if name == "long_link":
        link = "https://github.com/example/knowledge-search-platform/tree/main/docs/deployment-and-usage-guide"
        entry["details"][2].update(value=link, link=link, layout="row")
    if name == "large_metrics":
        entry["details"][0]["value"] = "123456"
        entry["details"][1]["value"] = "12345"
    if name == "inline_metrics":
        entry["details"] += [
            {"label": "用户", "value": "300 人", "layout": "inline", "emphasis": "bold"},
            {"label": "下载量", "value": "1200 次", "layout": "inline", "emphasis": "accent"},
        ]
    if name in {"body_phrase", "background", "local_edit", "edit_breaks_anchor"}:
        entry["highlights"] = [{"field": "text", "paragraph": 0, "text": PHRASE,
                                "emphasis": "bold_accent", "background": name == "background"}]
    if name == "field_emphasis":
        entry["highlights"] = [{"field": "organization", "emphasis": "bold_accent"},
                               {"field": "tech_stack", "text": "Python", "emphasis": "bold"}]
    if name == "missing_anchor":
        entry["highlights"] = [{"field": "text", "text": "原文没有这个片段", "emphasis": "bold"}]
    return {
        "person": {"name": "示例用户", "phone": "13800000000", "email": "demo@example.com"},
        "sections": [{"key": "projects", "title": "项目经历", "entries": [entry]}],
    }


def case_matrix():
    return [{"template": template, "scenario": name} for template in TEMPLATES for name in SCENARIOS]


def _xml(docx):
    from lxml import etree
    with ZipFile(docx) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    # Word 使用 Choice；未同步的兼容回退不是第二份可见内容。
    for fallback in root.iter(MC + "Fallback"):
        fallback.getparent().remove(fallback)
    return root


def phrase_styles(docx, phrase):
    """读取命中原文的每段 run 样式，含跨 run 的片段。"""
    rows = []
    for paragraph in _xml(docx).iter(W + "p"):
        # 外层 anchor 的宿主 run 包含 drawing/textbox，但并不拥有其中的正文。
        runs = [(run, "".join(node.text or "" for node in run.findall(W + "t")))
                for run in paragraph.iter(W + "r")
                if next(run.iterancestors(W + "p"), None) is paragraph]
        text = "".join(value for _, value in runs)
        start = text.find(phrase)
        if start < 0:
            continue
        cursor, matched = 0, []
        for run, value in runs:
            end = cursor + len(value)
            if cursor < start + len(phrase) and end > start:
                props = run.find(W + "rPr")
                def prop(tag, attr="val"):
                    node = props.find(W + tag) if props is not None else None
                    return node.get(W + attr) if node is not None else None
                matched.append({"text": value[max(0, start - cursor):min(len(value), start + len(phrase) - cursor)],
                                "bold": prop("b"), "color": prop("color"), "fill": prop("shd", "fill")})
            cursor = end
        rows.append(matched)
    return rows


def _check_styles(docx, content):
    checked = []
    for section in content["sections"]:
        for entry in section["entries"]:
            for mark in entry.get("highlights", []):
                target = mark.get("text") or entry[mark["field"]]
                hits = phrase_styles(docx, target)
                assert len(hits) == 1, f"强调锚点可见次数不为 1：{target}"
                for run in hits[0]:
                    if "bold" in mark["emphasis"]:
                        assert run["bold"] in {"1", "true", "on"}, (target, run)
                    if "accent" in mark["emphasis"]:
                        assert run["color"] and run["color"].upper() not in {"000000", "FFFFFF", "AUTO"}, (target, run)
                    if mark.get("background"):
                        assert run["fill"] and run["fill"] != "FFFFFF", (target, run)
                checked.append({"text": target, "runs": hits[0]})
            from skill_toolbox.resume_content import repository_links
            repos = repository_links(section, entry)
            for field in entry.get('details', []):
                emphasis = field.get('emphasis', 'normal')
                if emphasis == 'normal' and not field.get('background'):
                    continue
                repo = next((r for r in repos if r['label'] == field['label'] and r['value'] == field['value']), None)
                target = repo['text'] if repo else f"{field['label']}：{field['value']}"
                hits = phrase_styles(docx, target)
                assert len(hits) == 1, (target, hits)
                for run in hits[0]:
                    if emphasis in {'bold', 'bold_accent'}:
                        assert run['bold'] in {'1', 'true'}, (target, run)
                    if emphasis in {'accent', 'bold_accent'}:
                        assert run['color'] and run['color'].upper() not in {'000000', '595959', '808080'}, (target, run)
                    if field.get('background'):
                        assert run['fill'], (target, run)
    return checked


def verify_result(work, engine, result, expected):
    import pymupdf
    from skill_toolbox.contracts.resume_workflow import Content

    assert result.ok, result.as_dict()
    record = engine.store.load_revision(result.artifact_id, result.revision)
    assert record["mechanical"]["passed"] is True, record["mechanical"]
    assert result.data["fits_page_target"] is True
    # 比对完整外部内容，防止样式操作改字、改数、丢来源或链接。
    assert result.data["content"] == Content.model_validate(expected).model_dump()
    assert not result.data["content_review"]["warnings"], result.data["content_review"]
    docx, pdf = work / result.data["docx"], work / result.data["pdf"]
    requested_links = {f["link"] for s in expected["sections"] for e in s["entries"]
                       for f in e.get("details", []) if f.get("link")}
    with pymupdf.open(pdf) as document:
        links = {link.get("uri") for page in document for link in page.get_links()}
        assert requested_links <= links, (requested_links, links)
        pages = len(document)
    return {"candidate_id": result.data["candidate_id"], "mechanical": True,
            "content_and_sources_unchanged": True, "links": sorted(requested_links),
            "pages": pages, "highlight_runs": _check_styles(docx, expected),
            "docx": str(docx), "pdf": str(pdf), "visual": "not_run"}


def run_case(out, template, name):
    from skill_toolbox.materials import MaterialService
    from skill_toolbox.tools.materials import MaterialPlanService
    from skill_toolbox.tools.resume_edit import ResumeEditService
    from skill_toolbox.tools.resume_workflow import ResumeWorkflow
    from skill_toolbox.contracts.resume_workflow import GenerateRequest, EditRequest
    from skill_toolbox.contracts.common import ToolError

    work = out / template / name
    work.mkdir(parents=True, exist_ok=True)
    engine = ResumeEditService(work, ROOT / "backend/skill_toolbox/skill_defs/resume_pro/templates",
                               {"vision": True}, template_id=template)
    flow = ResumeWorkflow(engine, MaterialPlanService(work, MaterialService(work).catalog(),
                                                     "resume", {"vision": True}), template)
    content = case_content(name)
    flow.facts.add("fixture", json.dumps(content, ensure_ascii=False), kind="request")
    flow.facts.save()
    row = {"template": template, "scenario": name, "stages": []}
    try:
        result = flow.generate(GenerateRequest(content=content))
        if name == "missing_anchor":
            raise AssertionError("未匹配的锚点没有拒绝")
        row["stages"].append(verify_result(work, engine, result, content))
        if name == "edit_breaks_anchor":
            flow.edit(EditRequest(candidate_id=result.data["candidate_id"], changes=[{
                "op": "update_entry", "target_id": "projects#demo", "entry": {"text": ["改成不含原片段的内容。"]}}]))
            raise AssertionError("破坏已有锚点的修改没有拒绝")
        if name == "local_edit":
            content["sections"][0]["entries"][0]["text"][1] = "完善文档与回归测试。"
            result = flow.edit(EditRequest(candidate_id=result.data["candidate_id"], changes=[{
                "op": "update_entry", "target_id": "projects#demo",
                "entry": {"text": content["sections"][0]["entries"][0]["text"]}}]))
            row["stages"].append(verify_result(work, engine, result, content))
        if name == "undo":
            baseline = phrase_styles(work / result.data["docx"], PHRASE)
            for marks in ([{"field": "text", "text": PHRASE, "emphasis": "bold_accent", "background": True}], []):
                content["sections"][0]["entries"][0]["highlights"] = marks
                result = flow.edit(EditRequest(candidate_id=result.data["candidate_id"], changes=[{
                    "op": "update_entry", "target_id": "projects#demo", "entry": {"highlights": marks}}]))
                row["stages"].append(verify_result(work, engine, result, content))
            assert phrase_styles(work / result.data["docx"], PHRASE) == baseline, "撤销未恢复原样式"
            row["undo_restored"] = True
        row["passed"] = True
    except ToolError as exc:
        if name not in NEGATIVES or "HIGHLIGHT" not in exc.code:
            raise
        row.update(passed=True, rejection=exc.to_dict())
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="调用真实 Word；不调用 LLM")
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp_t/resume-highlights")
    parser.add_argument("--template", choices=TEMPLATES)
    parser.add_argument("--scenario", choices=SCENARIOS + NEGATIVES)
    args = parser.parse_args()
    cases = case_matrix() + [{"template": t, "scenario": n} for t in TEMPLATES for n in NEGATIVES]
    cases = [c for c in cases if (not args.template or c["template"] == args.template)
             and (not args.scenario or c["scenario"] == args.scenario)]
    if not args.execute:
        print(json.dumps({"execute": False, "positive_cases": 20, "negative_cases": 4,
                          "selected": cases, "note": "负例单独计数；不代表模型首稿成功率。"}, ensure_ascii=False, indent=2))
        return 0
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        try:
            row = run_case(args.output, case["template"], case["scenario"])
        except Exception as exc:
            row = {**case, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        report = {"positive": [r for r in results if r["scenario"] not in NEGATIVES],
                  "negative": [r for r in results if r["scenario"] in NEGATIVES],
                  "visual": "not_run", "model_trials": "not_run"}
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(not row["passed"] for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
