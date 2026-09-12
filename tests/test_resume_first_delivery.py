"""Deterministic delivery gates and fact-preserving edits; no model-quality claim."""
import asyncio
import json

import pytest

from skill_toolbox import runtime as rt
from skill_toolbox.contracts.resume_workflow import EditRequest
from skill_toolbox.models import AssistantTurn, ToolCall
from test_resume_workflow_service import generate, workflow  # noqa: F401


@pytest.mark.asyncio
async def test_workflow_first_delivery_requires_accept(workflow, tmp_path, monkeypatch):
    original_build = rt._build_domain_services
    events = []

    def build(*args, **kwargs):
        services = original_build(*args, **kwargs)
        engine = services.resume_v2
        engine.test_pages = 1
        engine.test_mechanical = True
        engine.test_render_count = 0
        return services

    monkeypatch.setattr(rt, "_build_domain_services", build)

    class Provider:
        calls = 0
        candidate = None
        path = None

        async def complete(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls == 1:
                name, args = "resume_generate", {"content": {
                    "person": {"name": "合成应聘者"},
                    "sections": [{"key": "skills", "title": "技能", "entries": [{"text": ["Python、SQL"]}]}],
                }}
            elif self.calls == 2:
                data = json.loads(messages[-1].tool_results[0].content)["data"]
                self.candidate, self.path = data["candidate_id"], data["docx"]
                name, args = "finish_task", {"artifacts": [self.path]}
            elif self.calls == 3:
                assert not messages[-1].tool_results[0].success
                assert not any(event["type"] == "task_completed" for event in events)
                name, args = "resume_accept", {"candidate_id": self.candidate, "visual_notes": "合成测试已检查候选全部页面。"}
            else:
                name, args = "finish_task", {"artifacts": [self.path]}
            return AssistantTurn(tool_calls=[ToolCall(id=f"call-{self.calls}", name=name, arguments=args)])

    provider = Provider()
    result = await asyncio.wait_for(rt.AgentRuntime(provider, events.append).run(rt.TaskRequest(
        skill_id="resume_pro", template_id=workflow.template_id, user_prompt="事实完整，直接生成并验收后交付。",
        output_dir=tmp_path / "delivery", capabilities={"vision": True, "tool_calling": True}, tool_mode="domain",
    )), timeout=15)
    assert result.status == "completed"
    assert provider.calls == 4, "未经 accept 的第一次 finish_task 必须被拒绝"
    assert sum(event["type"] == "task_completed" for event in events) == 1
    assert not any(event["type"] == "questions_requested" for event in events)


def test_semantic_summary_and_component_edits_preserve_given_facts(workflow):
    candidate = generate(workflow)
    edited = workflow.edit(EditRequest(candidate_id=candidate.data["candidate_id"], changes=[
        {"op": "update_entry", "target_id": "job-1", "entry": {"text": ["原职责；原成果"]}},
        {"op": "set_person_field", "field": {"key": "hometown", "label": "籍贯", "value": "测试省"}},
        {"op": "remove_person_field", "key": "phone"},
        {"op": "insert_section", "after_id": "internship", "section": {
            "key": "projects", "title": "项目经历", "entries": [{"id": "project-1", "text": ["课程项目负责测试，无量化指标。"]}],
        }},
        {"op": "remove_section", "section_id": "skills"},
    ]))
    content = edited.data["content"]
    assert content["person"]["name"] == candidate.data["content"]["person"]["name"]
    assert "phone" not in content["person"]
    assert any(field["key"] == "hometown" and field["label"] == "籍贯" and field["value"] == "测试省"
               for field in content["personal_fields"])
    assert [section["key"] for section in content["sections"]] == ["internship", "projects"]
    entry = content["sections"][0]["entries"][0]
    assert entry["text"] == ["原职责；原成果"]
    assert (entry["date"], entry["organization"], entry["role"]) == ("2024", "公司", "实习生")


def test_format_changes_presentation_without_semantic_text_mutation(workflow):
    candidate = generate(workflow)
    edited = workflow.edit(EditRequest(candidate_id=candidate.data["candidate_id"], changes=[
        {"op": "format", "scope": "all", "font_size_pt": 10.5, "scale": 0.95},
    ]))
    before, after = candidate.data["content"], edited.data["content"]
    assert after["person"] == before["person"]
    for old_section, section in zip(before["sections"], after["sections"], strict=True):
        assert (section["key"], section["title"]) == (old_section["key"], old_section["title"])
        for old_entry, entry in zip(old_section["entries"], section["entries"], strict=True):
            assert {key: entry[key] for key in ("id", "date", "organization", "role", "text")} == {
                key: old_entry[key] for key in ("id", "date", "organization", "role", "text")}
            assert entry["font_size_pt"] == 10.5
