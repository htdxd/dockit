import copy

from skill_toolbox.contracts.resume_workflow import Entry
from skill_toolbox.resume_content import canonical_entry


def test_explicit_stack_and_legacy_stack_have_same_canonical_shape():
    section = {"key": "projects", "title": "项目经历"}
    old = {"head": {"org": "个人项目 | 独立开发", "role": "学习平台"},
           "bullets": ["技术栈：Python、FastAPI", "独立实现资料解析与检索。"]}
    original = copy.deepcopy(old)
    result = canonical_entry(section, old)
    assert result["head"] == {"org": "学习平台", "role": "个人项目 | 独立开发"}
    assert result["tech_stack"] == "Python、FastAPI"
    assert result["bullets"] == ["独立实现资料解析与检索。"]
    assert old == original
    assert canonical_entry(section, result) == result


def test_native_stack_list_normalizes_without_losing_values():
    entry = Entry(organization="系统", role="公司项目", tech_stack=["Python", "SQL"])
    assert entry.tech_stack == "Python、SQL"
