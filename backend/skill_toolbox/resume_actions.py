"""六模板共用的内容编辑动作；纯内存操作，不访问 Word、文件或版本存储。"""
from dataclasses import dataclass
from typing import Any

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.resume import ResumeEditV2
from skill_toolbox.contracts.resume_workflow import Entry, Section
from skill_toolbox.resume_content import canonical_entry

V2_MIN_ENTRY_GAP_PT = 2.0
MIN_BODY_W_PT = 200.0
MAX_BODY_H_PT = 640.0


@dataclass(frozen=True)
class PreparedEdit:
    content: dict
    commands: list[ResumeEditV2]
    header: dict | None
    changes: list[dict]


class ResumeActions:
    def __init__(self, profile):
        self.profile = profile

    def person_fields(self, fields, *, custom_keys=()):
        """只归一化模板已声明的中文标签，保留明确的自定义字段标识。"""
        allowed = set(self.profile.header_labels.values()) | set(custom_keys)
        labels = {''.join(label.split()): key for label, key in self.profile.header_labels.items()}
        result = {}
        for name, value in fields.items():
            key = name if name in allowed else labels.get(''.join(name.split()), name)
            if key not in allowed:
                raise ToolError('FIELD_UNSUPPORTED', f'当前模板不支持个人信息字段 {name!r}；额外信息请用 personal_fields。')
            if key in result and result[key] != value:
                raise ToolError('CONTENT_INVALID', f'个人字段 {key} 提供了两个不同的值。')
            result[key] = value
        return result

    @staticmethod
    def next_entry_id(section: dict[str, Any]) -> str:
        """自动生成不冲突的条目 id（`<栏目key>-<n>`），并保证全局稳定可复现。"""
        used = {str(e.get("id")) for e in section.get("entries", [])}
        n = len(used) + 1
        while f"{section['key']}-{n}" in used:
            n += 1
        return f"{section['key']}-{n}"

    def apply(
        self, content: dict[str, Any], changes: list[Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        import copy

        new = copy.deepcopy(content)
        records: list[dict[str, Any]] = []
        for change in changes:
            op = change.op
            if op == "format_component":
                values = {key: getattr(change, key) for key in ("font_size_pt", "scale")
                          if getattr(change, key) is not None}
                if not values:
                    raise ToolError("CONTENT_INVALID", "格式调整需要 font_size_pt 或 scale。")
                if change.instance_id or change.entry_id:
                    _, entry = self.locate(new, change)
                    entry.update(values)
                else:
                    sec = self.section(new, change.section_key)
                    if "scale" in values:
                        sec["scale"] = values["scale"]
                    if "font_size_pt" in values:
                        for entry in sec["entries"]:
                            entry["font_size_pt"] = values["font_size_pt"]
                records.append({"op": op, "instance_id": change.instance_id,
                                "section": change.section_key, **values})
            elif op == "update_entry":
                sec, entry = self.locate(new, change)
                if change.entry is not None:
                    # 只覆盖**调用方显式给的**字段：pydantic 的默认值（head={}、
                    # bullets=[]）不代表“清空”，否则「只改 bullets」会把条目标题
                    # 槽整行抹掉（内容完整性检查会因此失败）。
                    provided = set(change.entry.model_fields_set) - {"id"}
                    payload = change.entry.model_dump(exclude_none=True)
                    for key in provided:
                        entry[key] = payload.get(key, entry.get(key))
                records.append({"op": op, "instance_id": change.instance_id,
                                "fields": sorted(set(change.entry.model_fields_set) - {"id"})
                                if change.entry is not None else []})
            elif op == "insert_entry":
                section_key = change.section_key or str(change.after or "").split("#", 1)[0]
                if not section_key:
                    raise ToolError(
                        "CONTENT_INVALID",
                        "insert_entry 需要 section_key（栏目 key，来自 resume_prepare_v2）"
                        "，或用 after=<section>#<entry> 指定插入位置。",
                    )
                sec = self.section(new, section_key)
                if change.entry is None:
                    raise ToolError("CONTENT_INVALID", "insert_entry 必须给 entry")
                entry = change.entry.model_dump(exclude_none=True)
                if not str(entry.get("id") or "").strip():
                    entry["id"] = self.next_entry_id(sec)
                elif any(e["id"] == entry["id"] for e in sec["entries"]):
                    raise ToolError(
                        "CONTENT_INVALID",
                        f"条目 id 已存在: {entry['id']}（省略 id 时后端自动生成）。",
                    )
                idx = self.index_of(sec, change.after) + 1 if change.after else len(sec["entries"])
                sec["entries"].insert(idx, entry)
                records.append({"op": op, "section": sec["key"], "entry_id": entry["id"]})
            elif op == "remove_entry":
                sec, entry = self.locate(new, change)
                sec["entries"] = [e for e in sec["entries"] if e is not entry]
                records.append({"op": op, "instance_id": change.instance_id})
            elif op == "move_entry":
                sec, entry = self.locate(new, change)
                sec["entries"] = [e for e in sec["entries"] if e is not entry]
                if change.before:
                    idx = self.index_of(sec, change.before)
                elif change.after:
                    idx = self.index_of(sec, change.after) + 1
                else:
                    idx = int(change.dy_pt) if change.dy_pt else len(sec["entries"])
                sec["entries"].insert(max(0, min(idx, len(sec["entries"]))), entry)
                records.append({"op": op, "instance_id": change.instance_id})
            elif op == "insert_section":
                if change.section is None:
                    raise ToolError("CONTENT_INVALID", "insert_section 必须给 section")
                sec = change.section.model_dump(exclude_none=True)
                target = change.before or change.after
                idx = len(new["sections"])
                if target:
                    anchor = self.section(new, target)
                    idx = new["sections"].index(anchor) + (0 if change.before else 1)
                new["sections"].insert(idx, sec)
                records.append({"op": op, "section": sec["key"]})
            elif op == "remove_section":
                new["sections"] = [
                    s for s in new["sections"] if s["key"] != change.section_key
                ]
                records.append({"op": op, "section": change.section_key})
            elif op == "update_section_title":
                sec = self.section(new, change.section_key)
                sec["title"] = change.title
                records.append({"op": op, "section": sec["key"], "title": change.title})
            elif op == "set_entry_gap":
                sec = self.section(new, change.section_key)
                gap = float(change.gap_pt or 0.0)
                if gap < V2_MIN_ENTRY_GAP_PT or gap > 24.0:
                    raise ToolError(
                        "BOUNDS_VIOLATION",
                        f"gap_pt={gap} 超出允许范围 [{V2_MIN_ENTRY_GAP_PT}, 24.0]",
                    )
                sec["entry_gap_pt"] = gap
                records.append({"op": op, "section": sec["key"], "gap_pt": gap})
            elif op in {"move_component", "resize_component"}:
                # 几何动作在布局结果上落地（见 _apply_geometry），此处只做参数
                # 校验并记录；**不支持的参数直接拒绝**，不返回成功却未执行。
                sec, entry = self.locate(new, change)
                if op == "move_component":
                    if change.width_pt is not None or change.height_pt is not None \
                            or change.height_delta_pt is not None:
                        raise ToolError(
                            "ACTION_NOT_ALLOWED",
                            "move_component 只接受 dx_pt/dy_pt；"
                            "宽高请用 resize_component（width_pt/height_pt/"
                            "height_delta_pt）。",
                        )
                    if abs(change.dx_pt) > 1e-9 and abs(change.dy_pt) > 1e-9:
                        raise ToolError(
                            "BOUNDS_VIOLATION",
                            "同一条动作禁止同时给 dx_pt 与 dy_pt（计划 §6.1）",
                        )
                    if change.dx_pt:
                        raise ToolError(
                            "ACTION_NOT_ALLOWED",
                            "正文组件不允许横向重定位（会改变文字测量宽度）；"
                            "如需变窄请用 resize_component(width_pt=…)。",
                        )
                    dy = float(change.dy_pt)
                    if not (-120.0 <= dy <= 120.0):
                        raise ToolError("BOUNDS_VIOLATION", f"dy_pt={dy} 超出 [-120, 120]")
                    if abs(dy) < 1e-9:
                        raise ToolError("CONTENT_INVALID", "move_component 的 dy_pt 不能为 0")
                    entry["dy_pt"] = float(entry.get("dy_pt", 0.0)) + dy
                    records.append({"op": op, "instance_id": change.instance_id, "dy_pt": dy})
                else:
                    if abs(change.dx_pt) > 1e-9 or abs(change.dy_pt) > 1e-9:
                        raise ToolError(
                            "ACTION_NOT_ALLOWED",
                            "resize_component 不接受 dx_pt/dy_pt；位移请用 "
                            "move_component（避免「用 dy_pt 当高度增量」的歧义）。",
                        )
                    if change.width_pt is None and change.height_pt is None \
                            and change.height_delta_pt is None:
                        raise ToolError(
                            "CONTENT_INVALID",
                            "resize_component 需要 width_pt / height_pt / "
                            "height_delta_pt 至少一个。",
                        )
                    if change.height_pt is not None and change.height_delta_pt is not None:
                        raise ToolError(
                            "BOUNDS_VIOLATION",
                            "height_pt 与 height_delta_pt 不能同时给（绝对/增量语义冲突）。",
                        )
                    record: dict[str, Any] = {"op": op, "instance_id": change.instance_id}
                    if change.width_pt is not None:
                        width = float(change.width_pt)
                        if not (MIN_BODY_W_PT <= width <= self.profile.body_width_pt):
                            raise ToolError(
                                "BOUNDS_VIOLATION",
                                f"width_pt={width} 超出 [{MIN_BODY_W_PT}, {self.profile.body_width_pt}]"
                                "（模板正文列宽为上限）。",
                            )
                        entry["width_pt"] = width
                        record["width_pt"] = width
                    if change.height_pt is not None:
                        height = float(change.height_pt)
                        if not (0.0 < height <= MAX_BODY_H_PT):
                            raise ToolError(
                                "BOUNDS_VIOLATION",
                                f"height_pt={height} 超出 (0, {MAX_BODY_H_PT}]。",
                            )
                        entry["height_pt"] = height
                        record["height_pt"] = height
                    if change.height_delta_pt is not None:
                        delta = float(change.height_delta_pt)
                        if not (0.0 <= delta <= 200.0):
                            raise ToolError(
                                "BOUNDS_VIOLATION",
                                f"height_delta_pt={delta} 超出 [0, 200]",
                            )
                        entry["height_delta_pt"] = (
                            float(entry.get("height_delta_pt", 0.0)) + delta
                        )
                        record["height_delta_pt"] = delta
                    records.append(record)
            else:
                raise ToolError("ACTION_UNKNOWN", f"不支持的动作: {op}")
        return new, records

    @staticmethod
    def section(content: dict[str, Any], key: str) -> dict[str, Any]:
        keys = [str(s.get("key")) for s in content.get("sections", [])]
        if not key or key not in keys:
            raise ToolError(
                "TARGET_NOT_FOUND",
                f"栏目不存在: {key!r}。当前栏目: {keys}",
                suggestion="insert_entry/remove_section/update_section_title/"
                           "set_entry_gap 用 section_key；"
                           "目标既有条目时直接用 instance_id（形如 <栏目key>#<条目id>）。",
            )
        return next(s for s in content["sections"] if s["key"] == key)

    @staticmethod
    def entry_ref(value: str) -> str:
        """实例引用归一化：`section#entry` / `entry` 都接受（before/after 亦然）。"""
        text = str(value or "")
        return text.split("#", 1)[1] if "#" in text else text

    @classmethod
    def index_of(cls, section: dict[str, Any], entry_id: str) -> int:
        if not entry_id:
            return len(section["entries"]) - 1
        wanted = cls.entry_ref(entry_id)
        for i, entry in enumerate(section["entries"]):
            if entry["id"] == wanted:
                return i
        raise ToolError("TARGET_NOT_FOUND", f"条目不存在: {entry_id}")

    def locate(
        self, content: dict[str, Any], change: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        instance_id = str(getattr(change, "instance_id", "") or "")
        section_key = str(getattr(change, "section_key", "") or "")
        entry_id = str(getattr(change, "entry_id", "") or "")
        if instance_id and "#" in instance_id:
            section_key, entry_id = instance_id.split("#", 1)
        elif instance_id and not section_key:
            section_key = instance_id
        section_key = section_key.split("#", 1)[0]
        sec = self.section(content, section_key)
        entry = sec["entries"][self.index_of(sec, entry_id)]
        return sec, entry

    @staticmethod
    def from_entry(entry, prototype, entry_id):
        head = {"date": entry.date, "org": entry.organization, "role": entry.role}
        return {
            "id": entry.id or entry_id,
            "source_ids": entry.source_ids,
            "tech_stack": entry.tech_stack,
            "details": [item.model_dump() for item in entry.details],
            "highlights": [item.model_dump() for item in entry.highlights],
            "head": head if any(head.values()) else {},
            "bullets": entry.text if prototype == "experience_v1" else [],
            "lines": entry.text if prototype == "plain_lines_v1" else [],
            "font_size_pt": entry.font_size_pt,
            "scale": entry.scale,
        }


    @classmethod
    def from_section(cls, section):
        # 纯文字栏目的重复栏目名不是经历标题，避免把它渲染成第二个标题。
        if section.key in {"skills", "summary"}:
            section = section.model_copy(update={"entries": [
                entry.model_copy(update={"organization": ""})
                if entry.organization == section.title and not entry.date and not entry.role else entry
                for entry in section.entries]})
        has_head = any(e.date or e.organization or e.role for e in section.entries)
        prototype = "experience_v1" if has_head else "plain_lines_v1"
        return {
            "key": section.key, "title": section.title, "prototype": prototype,
            "scale": section.scale,
            "entries": [cls.from_entry(e, prototype, f"{section.key}-{i}")
                        for i, e in enumerate(section.entries, 1)],
        }


    @staticmethod
    def entry_target(content, target):
        if "#" in target:
            return target
        for section in content["sections"]:
            if any(entry["id"] == target for entry in section["entries"]):
                return f"{section['key']}#{target}"
        raise ToolError("TARGET_NOT_FOUND", f"条目不存在: {target}")


    @staticmethod
    def to_sections(sections):
        return [Section(key=s["key"], title=s["title"], scale=s.get("scale"), entries=[Entry(
                id=e["id"], date=(e.get("head") or {}).get("date", ""),
                organization=(e.get("head") or {}).get("org", ""),
                role=(e.get("head") or {}).get("role", ""),
                text=e.get("bullets") or e.get("lines") or [], tech_stack=e.get("tech_stack", ""),
                details=e.get("details", []),
                highlights=e.get("highlights", []),
                font_size_pt=e.get("font_size_pt"), scale=e.get("scale"),
                source_ids=e.get("source_ids", []),
            ) for e in (canonical_entry(s, raw) for raw in s["entries"])]) for s in sections]


    def compile(self, content, changes, *, photo_changes=None):
        """将语义编辑转换并应用到副本；全部模板使用相同动作与字段转换。"""
        import copy

        working = copy.deepcopy(content)
        actions = []
        records = []
        header = copy.deepcopy(working.get("header") or {})
        header_changed = False
        hidden = set(header.get("hidden_fields") or [])
        custom = {item["key"]: item for item in header.get("custom_fields", [])}
        for change in changes:
            op = change.op
            if op == "format":
                if change.scope == "entry":
                    targets = [{"instance_id": self.entry_target(working, change.target_id)}]
                else:
                    selected = (working["sections"] if change.scope == "all" else
                                [self.section(working, change.target_id)])
                    targets = [{"section_key": section["key"]} for section in selected]
                edits = [ResumeEditV2(op="format_component", **target,
                                     font_size_pt=change.font_size_pt, scale=change.scale)
                         for target in targets]
                working, applied = self.apply(working, edits)
                records.extend(applied)
                actions.extend(edits)
                continue
            if op == "update_person":
                for key, value in self.person_fields(change.fields, custom_keys=custom).items():
                    if key in custom:
                        custom[key]["value"] = value
                        if key in header.get("fields", {}):
                            header["fields"][key] = value
                    else:
                        header.setdefault("fields", {})[key] = value
                    hidden.discard(key) if value.strip() else hidden.add(key)
                header_changed = True
                continue
            if op == "set_person_field":
                item = change.field.model_dump()
                custom[item["key"]] = item
                if item["key"] in header.get("fields", {}):
                    header["fields"][item["key"]] = item["value"]
                hidden.discard(item["key"]) if item["value"].strip() else hidden.add(item["key"])
                header_changed = True
                continue
            if op == "remove_person_field":
                if change.key not in header.get("fields", {}) and change.key not in custom:
                    raise ToolError("TARGET_NOT_FOUND", f"个人字段不存在: {change.key}")
                custom.pop(change.key, None)
                if change.key in header.get("fields", {}):
                    header["fields"][change.key] = ""
                    hidden.add(change.key)
                else:
                    hidden.discard(change.key)
                header_changed = True
                continue
            if op == "replace_photo":
                header.update(photo_changes[change.asset_id])
                header_changed = True
                continue
            if op == "move_section":
                section = copy.deepcopy(self.section(working, change.section_id))
                moved = [
                    ResumeEditV2(op="remove_section", section_key=change.section_id),
                    ResumeEditV2(op="insert_section", section=section,
                                 before=change.before_id or "", after=change.after_id or ""),
                ]
                working, applied = self.apply(working, moved)
                records.extend(applied)
                actions.extend(moved)
                continue
            if op == "update_entry":
                target = self.entry_target(working, change.target_id)
                section, current = self.locate(working, ResumeEditV2(
                    op=op, instance_id=target))
                external = self.to_sections([section])[0]
                old = next(e for e in external.entries if e.id == current["id"])
                patch = change.entry.model_dump(exclude_unset=True)
                for key in ("font_size_pt", "scale"):
                    if patch.get(key) is None:
                        patch.pop(key, None)
                entry = Entry.model_validate({**old.model_dump(), **patch})
                action = ResumeEditV2(op=op, instance_id=target,
                                     entry=self.from_entry(entry, section["prototype"], current["id"]))
            elif op == "insert_entry":
                section = self.section(working, change.section_id)
                used = {e["id"] for s in working["sections"] for e in s["entries"]}
                number = 1
                while f"{change.section_id}-{number}" in used:
                    number += 1
                entry = self.from_entry(change.entry, section["prototype"], f"{change.section_id}-{number}")
                action = ResumeEditV2(op=op, section_key=change.section_id,
                                     after=change.after_id or "", entry=entry)
            elif op in {"remove_entry", "move_entry"}:
                action = ResumeEditV2(op=op, instance_id=self.entry_target(working, change.target_id),
                                     before=getattr(change, "before_id", None) or "",
                                     after=getattr(change, "after_id", None) or "")
            elif op == "insert_section":
                action = ResumeEditV2(op=op, section=self.from_section(change.section), after=change.after_id or "")
            else:
                self.section(working, change.section_id)
                action = ResumeEditV2(op="update_section_title" if op == "rename_section" else op,
                                     section_key=change.section_id, title=getattr(change, "title", ""))
            working, applied = self.apply(working, [action])
            records.extend(applied)
            actions.append(action)
        # 整批结束再移除空栏；同批“删旧条目→插入新条目”仍可引用该栏目。
        for section in list(working["sections"]):
            if not section["entries"]:
                removal = ResumeEditV2(op="remove_section", section_key=section["key"])
                working, applied = self.apply(working, [removal])
                records.extend(applied)
                actions.append(removal)
        header["hidden_fields"] = sorted(hidden)
        header["custom_fields"] = list(custom.values())
        if header_changed:
            working['header'] = header
        return PreparedEdit(working, actions, header if header_changed else None, records)
