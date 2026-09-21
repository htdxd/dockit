"""将语义字段/正文锚点编译成段落字符范围，不修改内容。"""
from skill_toolbox.contracts.common import ToolError


def compile_highlights(entry, heading, slot_order, body_start):
    result = {}
    head = entry.get("head") or {}
    fields = {}
    cursor = 0
    for key in slot_order:
        value = str(head.get(key) or "").strip()
        if not value:
            continue
        start = heading.find(value, cursor)
        if start >= 0:
            fields[{"org": "organization"}.get(key, key)] = (value, 0, start)
            cursor = start + len(value)
    stack = entry.get("tech_stack", "")
    if stack:
        prefix = "" if stack.startswith(("技术栈：", "技术栈:")) else "技术栈："
        fields["tech_stack"] = (stack, len(heading.splitlines()), len(prefix))
    body = entry.get("bullets") or entry.get("lines") or []
    for item in entry.get("highlights", []):
        field = item["field"]
        if field == "text":
            index = item.get("paragraph", 0)
            value = body[index] if index < len(body) else ""
            # 空段不会排版，但仍按原始正文列表索引定位。
            paragraph = body_start + sum(len(p.splitlines()) for p in body[:index] if p.strip())
            offset = 0
        else:
            value, paragraph, offset = fields.get(field, ("", 0, 0))
        fragment = item.get("text") or value
        if not fragment or value.count(fragment) != 1:
            raise ToolError("HIGHLIGHT_TARGET_INVALID",
                            f"条目 {entry.get('id', '')} 的 {field} 强调片段未唯一匹配：{fragment!r}",
                            suggestion="使用当前原文和从0开始的正文段号；改字时同时更新 highlights，或传 [] 撤销。")
        start = offset + value.index(fragment)
        # heading/正文内容可能含换行，范围按真实渲染段落切分。
        full = heading if field in {"organization", "role", "date"} else (" " * offset + value)
        end = start + len(fragment)
        pos = 0
        for line_index, line in enumerate(full.split("\n")):
            left, right = max(start, pos), min(end, pos + len(line))
            if left < right:
                span = {**item, "start": left - pos, "end": right - pos}
                result.setdefault(paragraph + line_index, []).append(span)
            pos += len(line) + 1
    return result
