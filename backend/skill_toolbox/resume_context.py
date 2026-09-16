"""保留材料、用户问答和最新候选，避免历史简历与坏参数反复占据模型上下文。"""
import json

from skill_toolbox.models import ConversationMessage


def current_resume_context(messages):
    states = []
    prepared_ids = set()
    for message in messages:
        for result in message.tool_results:
            if not result.name.startswith("resume_") and result.name != "read_material":
                continue
            try:
                payload = json.loads(result.content)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            data = payload.get("data") or {}
            if result.name == "resume_prepare":
                prepared_ids.update(doc["material_id"] for doc in data.get("materials", []))
            states.append((result, payload, data))
    latest = next((data["candidate_id"] for _, _, data in reversed(states)
                   if data.get("candidate_id")), None)
    remove = set()
    failures = []
    for result, payload, data in states:
        candidate = data.get("candidate_id")
        if candidate and latest and candidate != latest:
            remove.add(result.tool_call_id)
        elif result.name == "read_material" and not result.images and data.get("view") == "blocks" \
                and data.get("material_id") in prepared_ids:
            remove.add(result.tool_call_id)
        elif result.name.startswith("resume_") and not candidate and payload.get("code"):
            remove.add(result.tool_call_id)
            failures.append((result.tool_call_id, payload))
    if not remove:
        return messages
    compact = []
    for message in messages:
        calls = [call for call in message.tool_calls if call.id not in remove]
        results = [result for result in message.tool_results if result.tool_call_id not in remove]
        if message.role == "tool" and not results:
            continue
        if message.role == "assistant" and message.tool_calls and not calls:
            continue
        compact.append(message.model_copy(update={"tool_calls": calls, "tool_results": results}))
    note = (f"当前有效候选：{latest}。历史候选和重复材料读取已省略；修改当前候选，不要重新生成以逃避同一排版问题。"
            if latest else "当前尚无已生成候选。请修正上次生成参数并调用 resume_generate；不要调用 edit/accept 或向用户索取 candidate_id。")
    if failures and states and failures[-1][0] == states[-1][0].tool_call_id:
        error = failures[-1][1]
        note += " 最近调用未通过：" + str(error.get("code")) + "；" + str(error.get("message", ""))
        note += "；" + str(error.get("suggestion") or "请按工具契约重新组织本次修改，勿复制历史坏参数。")
    compact.append(ConversationMessage(role="user", text=note))
    return compact
