"""项目标题与技术栈的语义分离；兼容旧候选，不依赖模板列位猜字段。"""
import re
from urllib.parse import urlsplit


def is_project(section):
    return "project" in str(section.get("key", section.get("id", ""))).lower() or "项目" in section.get("title", "")


def inline_metrics(section, entry):
    return [field for field in entry.get("details", [])
            if is_project(section) and field["label"].strip().lower() in {"stars", "star", "forks", "fork"}]


def repository_links(section, entry):
    """仅缩短 GitHub 展示文字，完整网址和原始语义内容保持不变。"""
    result = []
    if not is_project(section):
        return result
    for field in entry.get("details", []):
        if field in inline_metrics(section, entry):
            continue
        if field.get("layout", "auto") == "row":
            continue
        url = field.get("link") or field.get("value", "")
        parsed = urlsplit(url)
        parts = parsed.path.strip('/').split('/')
        if parsed.scheme in {"http", "https"} and parsed.hostname in {"github.com", "www.github.com"} and len(parts) >= 2:
            result.append({**field, "link": url, "text": '/'.join(parts[:2]).removesuffix('.git')})
    return result


def title_metrics(section, entry):
    fields = [{**f, "text": f"{f['label']}：{f['value']}"} for f in inline_metrics(section, entry)]
    return repository_links(section, entry) + fields


def detail_rows(section, entry):
    inline = inline_metrics(section, entry)
    repos = repository_links(section, entry)
    return [field for field in entry.get("details", []) if field not in inline
            and not any(all(repo.get(k) == v for k, v in field.items() if k != 'link') for repo in repos)]


def detail_groups(section, entry):
    """相邻 inline 字段同行，保持用户给定顺序；独立字段不会被越过。"""
    groups = []
    for field in detail_rows(section, entry):
        if field.get("layout") == "inline" and groups and groups[-1][-1].get("layout") == "inline":
            groups[-1].append(field)
        else:
            groups.append([field])
    return groups


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
