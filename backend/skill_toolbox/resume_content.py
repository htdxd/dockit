"""项目标题与技术栈的语义分离；兼容旧候选，不依赖模板列位猜字段。"""
import re


def is_project(section):
    return "project" in str(section.get("key", section.get("id", ""))).lower() or "项目" in section.get("title", "")


def canonical_entry(section, entry):
    if not is_project(section):
        return entry
    result = dict(entry)
    head = dict(entry.get("head") or {})
    result["head"] = head
    kind = r"^(?:个人项目|公司项目|课程项目|开源项目)(?:\s*[|｜]|$)"
    # 早期 t001 候选曾按模板列位反填字段；仅识别明确的项目性质标签。
    if re.match(kind, head.get("org", "")) and not re.match(kind, head.get("role", "")):
        head["org"], head["role"] = head.get("role", ""), head["org"]
    legacy_stacks = []
    role = head.get("role", "")
    if re.match(kind, role) and " · " in role:
        head["role"], stack = role.split(" · ", 1)
        legacy_stacks.append(stack.strip())
    body_key = "bullets" if entry.get("bullets") else "lines"
    body = []
    for paragraph in entry.get(body_key) or []:
        match = re.match(r"^\s*技术栈\s*[:：]\s*(.+)$", paragraph, re.S)
        if match:
            legacy_stacks.append(match.group(1).strip())
        else:
            body.append(paragraph)
    result[body_key] = body
    explicit = entry.get("tech_stack")
    result["tech_stack"] = explicit or "；".join(dict.fromkeys(legacy_stacks))
    return result
