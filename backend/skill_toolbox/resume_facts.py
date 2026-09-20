"""任务内事实来源与候选对照；记录原文，不把匹配成功当成真实性证明。"""
import json
import re
from decimal import Decimal

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.tools.workspace import atomic_write_json

WRITING_STYLES = {
    "light": "轻度润色：保留原有结构与表达重点，修正语病、统一措辞；从零生成时按事实简洁组织。",
    "balanced": "突出优势：按岗位突出本人贡献与真实成果，合并重复，弱化无关细节，保留必要时间线。",
    "strong": "深度改写：可以重组栏目与经历叙述、精选相关事实、强化动作与结果的表达，但不能升级职责或补写未提供的功能、测试、协作流程和业绩。",
}


def quantities(text):
    return {format(Decimal(match.group(1)).normalize(), "f") + match.group(2)
            for match in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(%|倍|万|亿|人|项|篇|个)",
                                    text.replace("％", "%"))}


class ResumeFacts:
    def __init__(self, workspace):
        self.path = workspace / "work/resume/facts.json"
        self.sources = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def add(self, source_id, text, **location):
        self.sources[source_id] = {"text": text, **location}

    def save(self):
        atomic_write_json(self.path, self.sources)

    def writing_focus(self):
        text = "\n".join(source["text"] for source in self.sources.values())
        hints = []
        if any(word in text for word in ("科研项目", "研究经历", "研究生", "投稿", "在投", "在审", "预印本", "专利")):
            hints.append("科研：区分问题、本人方法/实验、发现与成果状态；在投不等于录用，预印本不等于发表，作者位次不能升级。")
        if any(word in text for word in ("转行", "转岗", "转专业")):
            hints.append("转向岗位：从真实旧经历找可迁移的行动和成果；保留时间线，不改写为已有目标岗位任职经历。")
        if any(word in text for word in ("课程", "课设", "无实习", "没有实习", "竞赛", "开源")):
            hints.append("实践：课程/个人/竞赛/开源来源与企业经历分开；没有实习可以省略，没获奖的参赛作品也可写，提交 PR 不等于已合并。")
        return hints

    def answer(self, questions, answers):
        source_id = f"answer-{1 + sum(key.startswith('answer-') for key in self.sources)}"
        # 问题提供语境，但问题中的示例不是用户事实。
        self.add(source_id, json.dumps(answers, ensure_ascii=False), kind="answer", questions=questions)
        self.save()
        return source_id

    def validate_refs(self, sections):
        unknown = {ref for section in sections for entry in section["entries"]
                   for ref in entry.get("source_ids", []) if ref not in self.sources}
        if unknown:
            raise ToolError("SOURCE_UNKNOWN", f"来源 ID 不存在：{sorted(unknown)}；复制 prepare 返回的 source_id 或问答的 source_id。")

    def review(self, content):
        entries, warnings = [], []
        for section in content["sections"]:
            for entry in section["entries"]:
                refs = entry.get("source_ids") or list(self.sources)
                source_text = "\n".join(self.sources[ref]["text"] for ref in refs if ref in self.sources)
                text = "\n".join([*(entry.get("head") or {}).values(),
                                  *(f"{f['label']}：{f['value']}" for f in entry.get("details", [])),
                                  *entry.get("bullets", []), *entry.get("lines", [])])
                target = f"{section['key']}#{entry['id']}"
                entries.append({"target_id": target, "source_ids": refs,
                                "reference_scope": "selected" if entry.get("source_ids") else "task",
                                "text": text})
                if source_text:
                    organization = (entry.get("head") or {}).get("org", "").strip()
                    if organization and ("education" in section["key"] or "教育" in section.get("title", "")):
                        names = re.findall(r"[\u4e00-\u9fff]{2,20}(?:大学|学院|学校)", source_text)
                        names = [re.sub(r"^(?:学校是|学校为|就读于|毕业于|就读|来自)", "", name) for name in names]
                        longer = sorted({name for name in names if name != organization and name.endswith(organization)})
                        if longer:
                            warnings.append({"target_id": target, "organization_to_check": organization,
                                             "source_names": longer, "message": "学校名称可能被截短，请对照原文保留全称。"})
                    added = sorted(quantities(text) - quantities(source_text))
                    if added:
                        warnings.append({"target_id": target, "quantities_to_check": added})
                    # 仅提示高影响的职责用语，避免把所有动词变成硬性禁词。
                    upgrades = [word for word in ("独立", "主导", "核心成员", "负责人", "精通")
                                if word in text and word not in source_text]
                    if upgrades:
                        warnings.append({"target_id": target, "responsibility_to_check": upgrades,
                                         "message": "来源中未见此职责/熟练程度；确认是否只是换说法，否则退回有依据的表述。不要为修辞问题追问用户。"})
        return {"entries": entries, "warnings": warnings, "semantic_review": "agent_required",
                "note": "数字差异仅提示复核，等价换算也可能触发；来源命中不证明身份、状态、贡献或指标口径正确。对照原文与用户回答，不用问题示例补事实。"}
