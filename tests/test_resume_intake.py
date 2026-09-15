"""Verify resume prompt selection and real broker wiring, not model judgement."""
import asyncio
import json

import pytest

from skill_toolbox.contracts.common import OperationResult
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest, UserInputBroker
from skill_toolbox.tools.resume_edit import ResumeEditService


class RecordingProvider(ScriptedProvider):
    def __init__(self, turns):
        super().__init__(turns)
        self.calls = []

    async def complete(self, system_prompt, messages, tools):
        self.calls.append((system_prompt, [message.model_copy(deep=True) for message in messages], tools))
        return await super().complete(system_prompt, messages, tools)


def turn(name, arguments):
    return AssistantTurn(tool_calls=[ToolCall(id=name, name=name, arguments=arguments)])


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_resume", [False, True], ids=["from-scratch", "vague-optimization"])
async def test_resume_intake_prepare_questions_answers_use_same_runtime(tmp_path, monkeypatch, existing_resume):
    # Only skip template geometry probing; material ingestion, workflow prepare,
    # tool dispatch, question events and UserInputBroker all run unchanged.
    monkeypatch.setattr(ResumeEditService, "prepare", lambda self, template_id: OperationResult(
        ok=True, data={"header_fields": ["name", "phone", "education"]}))
    materials = []
    facts = "姓名：测试候选人。籍贯：测试省。教育：测试大学，在读。\n" + "参与课程项目，负责需求整理与测试。" * 30
    if existing_resume:
        source = tmp_path / "existing-resume.txt"
        source.write_text(facts, encoding="utf-8")
        materials.append(source)
        prompt = "帮我优化这份简历。"
        questions = [
            {"id": "target", "label": "希望申请什么岗位？", "type": "text"},
            {"id": "edits", "label": "希望如何优化，是否允许摘要？", "type": "textarea"},
        ]
        answers = {"target": "测试分析岗位", "edits": "允许摘要为一页，保留项目事实。"}
    else:
        prompt = "我还没有简历，帮我开始制作。"
        questions = [
            {"id": "target", "label": "目标岗位是什么？", "type": "text"},
            {"id": "background", "label": "请介绍教育和真实经历。", "type": "textarea"},
        ]
        answers = {"target": "测试分析岗位", "background": "测试大学在读，课程项目负责测试；不展示联系方式。"}
    provider = RecordingProvider([
        turn("resume_prepare", {}),
        turn("ask_user_questions", {"questions": questions}),
        turn("task_failed", {"error": "测试在收集回答后停止，不执行 Word。"}),
    ])
    broker = UserInputBroker()
    events = []

    def emit(event):
        events.append(event)
        if event["type"] == "questions_requested":
            # Same asynchronous delivery shape as a desktop answer arriving later.
            asyncio.get_running_loop().call_soon(broker.answer, answers)

    runtime = AgentRuntime(provider=provider, emit=emit, input_broker=broker)
    result = await asyncio.wait_for(runtime.run(TaskRequest(
        skill_id="resume_pro", template_id="t001", user_prompt=prompt,
        output_dir=tmp_path / "out", materials=materials,
        capabilities={"vision": True, "tool_calling": True}, tool_mode="domain",
        writing_style="strong",
    )), timeout=15)
    assert result.status == "failed" and "测试在收集回答后停止" in result.error
    assert len(provider.calls) == 3
    system_prompt = provider.calls[0][0]
    assert "两种问答入口" in system_prompt
    assert "本次写作档位：深度改写" in system_prompt
    assert "ask_user_questions" in system_prompt
    assert "用户明确指定" in system_prompt and "直接执行" in system_prompt
    assert "籍贯不等于现居住址" in system_prompt
    names = {tool["name"] for tool in provider.calls[0][2]}
    assert {"resume_prepare", "resume_edit", "ask_user_questions"} <= names
    assert "resume_generate_v2" not in names and "create_content_plan" not in names
    prepared_result = next(result for message in provider.calls[1][1] for result in message.tool_results
                           if result.name == "resume_prepare")
    prepared = json.loads(prepared_result.content)["data"]
    assert prepared["template_id"] == "t001"
    if existing_resume:
        returned = "\n".join(block["text"] for block in prepared["materials"][0]["blocks"])
        assert "".join(returned.split()) == "".join(facts.split())
    else:
        assert prepared["materials"] == []
    answered = next(result for message in provider.calls[2][1] for result in message.tool_results
                    if result.name == "ask_user_questions")
    assert answered.success and json.loads(answered.content) == {"answers": answers, "source_id": "answer-1"}
    assert [event["questions"] for event in events if event["type"] == "questions_requested"] == [questions]
    assert any(event["type"] == "questions_answered" and event["answer_ids"] == list(answers) for event in events)
