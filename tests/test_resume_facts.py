import pytest

from skill_toolbox.resume_facts import ResumeFacts, quantities
from skill_toolbox.contracts.common import ToolError


def content(text, refs=None):
    return {"sections": [{"key": "projects", "entries": [
        {"id": "p1", "lines": [text], "source_ids": refs or []}]}]}


def test_quantities_keep_units_and_chinese_context():
    assert quantities("准确率93.0％，增长20%，完成2项，0.5倍") == {"93%", "20%", "2项", "0.5倍"}


def test_answer_examples_are_not_evidence(tmp_path):
    facts = ResumeFacts(tmp_path)
    ref = facts.answer([{"question": "例如效率提升30%？"}], {"q1": "没有统计，只完成了测试"})
    result = facts.review(content("效率提升30%", [ref]))
    assert result["warnings"][0]["quantities_to_check"] == ["30%"]
    assert ResumeFacts(tmp_path).sources[ref]["questions"]
    assert facts.review(content("完成功能测试", [ref]))["warnings"] == []


def test_selected_sources_do_not_borrow_other_project_metrics(tmp_path):
    facts = ResumeFacts(tmp_path)
    facts.add("p1", "课程项目，完成功能测试")
    facts.add("p2", "另一项目准确率93%")
    review = facts.review(content("准确率93%", ["p1"]))
    assert review["warnings"]
    assert review["semantic_review"] == "agent_required"
    assert facts.review(content("准确率93%", ["p2"]))["warnings"] == []
    with pytest.raises(ToolError, match="来源 ID"):
        facts.validate_refs(content("正文", ["unknown"])["sections"])


def test_missing_refs_keep_task_scope_without_claiming_semantic_pass(tmp_path):
    facts = ResumeFacts(tmp_path)
    facts.add("request", "未获奖的课程项目")
    result = facts.review(content("获奖项目"))
    assert result["entries"][0]["reference_scope"] == "task"
    assert result["semantic_review"] == "agent_required"
    assert facts.writing_focus()


def test_role_upgrade_is_a_targeted_review_hint_not_an_automatic_verdict(tmp_path):
    facts = ResumeFacts(tmp_path)
    facts.add("request", "小组项目，负责借阅接口")
    result = facts.review(content("核心成员，独立实现借阅接口"))
    assert set(result["warnings"][0]["responsibility_to_check"]) == {"核心成员", "独立"}
    assert result["semantic_review"] == "agent_required"


def test_shortened_school_name_has_source_hint(tmp_path):
    facts = ResumeFacts(tmp_path)
    facts.add("request", "示例理工学院计算机科学本科在读")
    data = {"sections": [{"key": "education", "entries": [
        {"id": "e1", "head": {"org": "理工学院"}, "source_ids": ["request"]}]}]}
    assert facts.review(data)["warnings"][0]["source_names"] == ["示例理工学院"]
    data["sections"][0]["entries"][0]["head"]["org"] = "示例理工学院"
    assert not facts.review(data)["warnings"]
