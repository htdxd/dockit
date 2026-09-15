"""长标题列不再通过制表符挤出文本框；逻辑标题段仍保留标题样式。"""
from skill_toolbox.resume_layout import emit
from skill_toolbox.resume_layout.t001 import body_for
from skill_toolbox.resume_layout.t109 import TEMPLATE
from skill_toolbox.tools.resume_edit import ResumeEditService


def test_long_heading_stacks_without_dropping_text(tmp_path):
    engine = ResumeEditService(tmp_path, TEMPLATE.parent.parent, {}, template_id="t001")
    entry = {"id": "project", "head": {"org": "OpenLearning 多 Agent 智能学习平台",
             "role": "个人项目 | 独立开发 · " + "Multi-Agent, MCP, Skills, FastAPI, TypeScript, " * 3},
             "bullets": ["正文职责与成果保持不变。"]}
    scenario = engine._scenario_from_content({"sections": [{"key": "projects", "title": "项目经历", "entries": [entry]}]})
    data = scenario["sections"][0]["entries"][0]
    assert data["heading_lines"] == 1 and data["tech_stack_line"] == 1
    assert data["text"].splitlines() == [entry["head"]["org"] + "\t个人项目 | 独立开发",
        "技术栈：" + entry["head"]["role"].split(" · ", 1)[1].strip(), entry["bullets"][0]]
    root = emit.load_document_xml(TEMPLATE.parent.parent / "t001/template.docx")
    body = body_for(root, {"id": "work"})
    emit.set_box_text(body, data["text"], first_is_header=True, header_lines=1, tech_stack_line=1)
    paragraphs = body.findall(".//" + emit.W + "txbxContent/" + emit.W + "p")
    assert paragraphs[0].find(emit.W + "pPr/" + emit.W + "numPr") is None
    assert paragraphs[1].find(emit.W + "pPr/" + emit.W + "numPr") is None
    assert paragraphs[2].find(emit.W + "pPr/" + emit.W + "numPr") is not None


def test_non_project_columns_keep_the_existing_template_layout(tmp_path):
    engine = ResumeEditService(tmp_path, TEMPLATE.parent.parent, {}, template_id="t109")
    entry = {"head": {"date": "2023.09 - 至今", "org": "测试大学",
                      "role": "自动化（本科）｜ 男 ｜ 22岁 ｜ 应届生"}}
    assert engine._entry_heading({"key": "education", "title": "教育背景"}, entry) == (
        "2023.09 - 至今\t测试大学\t自动化（本科）｜ 男 ｜ 22岁 ｜ 应届生")
