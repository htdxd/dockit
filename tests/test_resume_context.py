"""上下文按候选状态压缩，保留原材料/问答/最新页图及合法工具配对。"""
import json

from skill_toolbox.models import ConversationMessage, ToolCall, ToolResult, ImageContent
from skill_toolbox.resume_context import current_resume_context


def pair(ident, name, payload, arguments=None, image=False):
    return [ConversationMessage(role="assistant", tool_calls=[ToolCall(id=ident, name=name, arguments=arguments or {})]),
            ConversationMessage(role="tool", tool_results=[ToolResult(
                tool_call_id=ident, name=name, success="code" not in payload,
                content=json.dumps(payload, ensure_ascii=False), images=[ImageContent(media_type="image/png", base64_data=ident)] if image else [])])]


def test_latest_candidate_and_all_its_preview_pages_replace_old_states():
    messages = [ConversationMessage(role="user", text="保留所有项目，只改技能。")]
    messages += pair("source", "resume_prepare", {"data": {"materials": [{"material_id": "m", "blocks": [{"text": "原材料完整事实"}]}]}})
    messages += pair("duplicate", "read_material", {"data": {"material_id": "m", "view": "blocks", "blocks": [{"text": "原材料完整事实"}]}})
    messages += pair("qa", "ask_user_questions", {"data": "真实回答，不能被省略"})
    messages += pair("old", "resume_generate", {"data": {"candidate_id": "r@1", "content": "旧稿"}}, image=True)
    messages += pair("new", "resume_edit", {"data": {"candidate_id": "r@2", "content": "当前完整状态"}}, image=True)
    messages += pair("page2", "resume_preview", {"data": {"candidate_id": "r@2", "content": "当前完整状态"}}, image=True)
    original = [message.model_dump_json() for message in messages]
    result = current_resume_context(messages)
    calls = {call.id for message in result for call in message.tool_calls}
    outputs = {item.tool_call_id: item for message in result for item in message.tool_results}
    assert calls == set(outputs) == {"source", "qa", "new", "page2"}
    assert outputs["new"].images and outputs["page2"].images
    assert "真实回答" in outputs["qa"].content
    assert [message.model_dump_json() for message in messages] == original


def test_bad_arguments_are_not_replayed_but_latest_error_is_explained():
    messages = pair("source", "resume_prepare", {"data": {"materials": []}})
    messages += pair("draft", "resume_generate", {"code": "PAGE_TARGET_EXCEEDED", "data": {"candidate_id": "r@1", "content": "完整文字"}}, image=True)
    for index in range(8):
        messages += pair(str(index), "resume_edit", {"code": "TOOL_ARGUMENTS_INVALID", "message": "changes 在位置17多余括号"},
                         {"changes": "BAD_BRACKET" * 100})
    result = current_resume_context(messages)
    encoded = "".join(message.model_dump_json() for message in result)
    assert "BAD_BRACKET" not in encoded
    assert "changes 在位置17多余括号" in result[-1].text
    assert "PAGE_TARGET_EXCEEDED" in encoded and "完整文字" in encoded
    assert len(encoded) < sum(len(message.model_dump_json()) for message in messages) / 3
