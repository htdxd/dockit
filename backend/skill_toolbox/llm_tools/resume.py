"""简历可见工具 schema（实施计划 §5.3）。

changes[] 只允许有限字段（component_id/text/move_rows/resize_rows/asset_id）；
move_rows/resize_rows 由后端换算为安全几何动作。
"""

from __future__ import annotations

from skill_toolbox.llm_tools.common import function_schema


def _legacy_tools() -> list[dict]:
    _change = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["replace_text", "replace_asset", "shift_components", "resize_component"], "description": "受限动作"},
            "component_id": {"type": "string", "description": "模板 components[].id（见 resume_prepare 返回）"},
            "text": {"type": "string", "description": "replace_text 的新文本"},
            "move_rows": {"type": "integer", "description": "shift_components：纵向移动行数（后端换算为安全 pt）"},
            "resize_rows": {"type": "integer", "description": "resize_component：扩展高度行数（后端换算为安全 pt）"},
            "asset_id": {"type": "string", "description": "replace_asset：真实 asset_id 或 __remove__"},
        },
        "required": ["action", "component_id"],
        "additionalProperties": False,
    }
    return [
        function_schema(
            "resume_prepare",
            "校验模板 hash 并返回字段/组件 schema、容量与白名单动作。"
            "template_id 必须来自 initial_form 的模板选项。",
            {
                "template_id": {"type": "string", "description": "模板 id（t001/t002/t026/t046/t109）"},
            },
            ["template_id"],
        ),
        function_schema(
            "resume_generate",
            "从材料抽取的字段填充模板并生成简历 DOCX。字段 key 必须来自模板 manifest"
            "（resume_prepare 返回）；照片传 photo_asset_id（真实 asset_id）。"
            "后端自动处理溢出缩号与机械检查；有 overflow_risks 时按提示精简内容后重试。",
            {
                "template_id": {"type": "string", "description": "模板 id"},
                "fields": {"type": "object", "description": "字段 key → 值（key 来自模板 manifest）"},
                "photo_asset_id": {"type": "string", "description": "可选：照片的真实 asset_id"},
                "target_role": {"type": "string", "description": "可选：目标岗位"},
                "formats": {"type": "array", "items": {"type": "string", "enum": ["docx"]}, "description": "可选输出格式，当前仅支持 docx"},
            },
            ["template_id", "fields"],
        ),
        function_schema(
            "resume_repair",
            "只允许 manifest 白名单内的字段替换/组件移动/扩高动作；"
            "候选副本失败自动回滚。move_rows/resize_rows 由后端转换为安全几何动作并检查边界与重叠。",
            {
                "artifact_id": {"type": "string", "description": "resume_generate 返回的 artifact_id"},
                "changes": {"type": "array", "items": _change, "description": "受限动作列表（1-8 个）"},
            },
            ["artifact_id", "changes"],
        ),
    ]


def _entry_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "条目 id（同栏目内唯一；省略时后端按 <栏目>-<序号> 生成）"},
            "head": {
                "type": "object",
                "description": "条目标题槽位（experience_v1）：date/org/role。空槽位留空，不写空白字符。",
                "properties": {
                    "date": {"type": "string"},
                    "org": {"type": "string"},
                    "role": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "bullets": {
                "type": "array", "items": {"type": "string"},
                "description": "职责要点（每项一段，带项目符号）；最多 8 项、每项 ≤400 字",
            },
            "lines": {
                "type": "array", "items": {"type": "string"},
                "description": "plain_lines_v1 的连排正文行（无项目符号）",
            },
        },
        "additionalProperties": False,
    }


def _section_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "栏目实例键（稳定、可复现，不用数组下标）"},
            "title": {"type": "string", "description": "栏目标题（含括号英文，如 实习经历（Internship））"},
            "prototype": {"type": "string", "enum": ["experience_v1", "plain_lines_v1"],
                          "description": "组件原型：经历型（标题槽位+项目符号）/ 连排型"},
            "entry_gap_pt": {"type": "number", "description": "可选：本栏条目间距（2-24pt）"},
            "entries": {"type": "array", "items": _entry_schema()},
        },
        "required": ["key", "title", "prototype", "entries"],
        "additionalProperties": False,
    }


def _edit_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": [
                "update_entry", "insert_entry", "remove_entry", "move_entry",
                "insert_section", "remove_section", "update_section_title",
                "set_entry_gap", "move_component", "resize_component",
            ]},
            "instance_id": {"type": "string", "description": "目标实例，形如 internship#intern-1（来自 resume_prepare_v2）"},
            "section_key": {"type": "string", "description": "栏目 key（新增/删除栏目、改标题、设间距时用）"},
            "entry_id": {"type": "string", "description": "条目 id（部分动作可直接给）"},
            "entry": _entry_schema(),
            "section": _section_schema(),
            "title": {"type": "string", "description": "update_section_title 的新标题（≤40 字）"},
            "after": {"type": "string", "description": "插入/移动到该实例之后"},
            "before": {"type": "string", "description": "移动到该实例之前"},
            "gap_pt": {"type": "number", "description": "set_entry_gap：条目间距（2-24pt）"},
            "dx_pt": {"type": "number", "description": "move_component 横向位移（正文恒为 0；传非 0 会被拒绝）"},
            "dy_pt": {"type": "number", "description": "move_component 纵向位移（-120..120pt，不能为 0）；resize_component 不接受 dy_pt。reflow 下移/加高会推动后续条目、后续栏目并在必要时分页；local 只动目标条目，碰撞即整批失败"},
            "width_pt": {"type": "number", "description": "resize_component：正文框宽度绝对值（200..532.5pt）；变窄会重新测量换行并推动后续组件"},
            "height_pt": {"type": "number", "description": "resize_component：正文框高度绝对值（不得小于文字需要的高度）"},
            "height_delta_pt": {"type": "number", "description": "resize_component：高度增量（0..200pt）；与 height_pt 二选一"},
        },
        "required": ["op"],
        "additionalProperties": False,
    }


def _header_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": "个人信息区字段：name/ethnicity/phone/email/address/birth/"
                               "height/politics/school/degree（键必须来自 resume_prepare_v2."
                               "header_fields；未知键会被显式拒绝，不会静默忽略）",
                "additionalProperties": {"type": "string"},
            },
            "photo_path": {"type": "string", "description": "可选：workspace 相对路径的照片（等比适配原照片框，不裁切）"},
        },
        "additionalProperties": False,
    }


def resume_v2_tools() -> list[dict]:
    """t109 v2：版本化编辑 + 自动渲染返回预览图（计划 §6.1–6.3）。

    一次调用 = 原子应用动作 → 测量 → 重排 → 渲染 → 机械检查 → 候选版本，
    并把受影响页的图像作为工具结果返回给模型（有 vision 时）。
    """
    return [
        function_schema(
            "resume_prepare_v2",
            "查看 t109 的 v2 能力：组件原型、动作白名单与上下限、栏目/条目实例（含 region 与行数）。"
            "编辑既有产物时传 artifact_id（可选 revision）获取当前实例模型。",
            {
                "template_id": {"type": "string", "description": "模板 id，当前 v2 认证 t109"},
                "artifact_id": {"type": "string", "description": "可选：既有产物 id"},
                "revision": {"type": "integer", "description": "可选：查看指定版本（默认当前）"},
            },
            ["template_id"],
        ),
        function_schema(
            "resume_generate_v2",
            "用版本化内容（v2）生成 t109 简历：测量真实高度 → 顺序重排 → 必要时分页 → 渲染预览。"
            "返回 candidate_revision、页数与受影响页预览图；机械检查不过的候选会被标记为不可接受。",
            {
                "template_id": {"type": "string", "description": "模板 id（t109）"},
                "content": {
                    "type": "object",
                    "description": "v2 内容：{schema_version:'resume-content-v2', template_id, layout_mode, header, sections:[…]}",
                    "properties": {
                        "schema_version": {"type": "string", "enum": ["resume-content-v2"]},
                        "template_id": {"type": "string"},
                        "layout_mode": {"type": "string", "enum": ["reflow", "local"]},
                        "header": _header_schema(),
                        "sections": {"type": "array", "items": _section_schema()},
                    },
                    "required": ["sections"],
                    "additionalProperties": False,
                },
                "request_id": {"type": "string", "description": "幂等键；同 id 同载荷重试返回原结果"},
                "layout_mode": {"type": "string", "enum": ["reflow", "local"], "description": "默认 reflow"},
            },
            ["template_id", "content"],
        ),
        function_schema(
            "resume_repair_v2",
            "在一批 changes 上做原子编辑并预览：先全量校验，再在隔离候选上按序应用，"
            "最后统一测量/重排/渲染/检查；任何非法动作或最终越界令整批失败且不改动已接受版本。"
            "失败时返回该候选的预览图供你判断（候选不可接受、不可交付）。",
            {
                "artifact_id": {"type": "string", "description": "resume_generate_v2 返回的 artifact_id"},
                "base_revision": {"type": "integer", "description": "必须是当前可编辑版本（过期返回 VERSION_CONFLICT）"},
                "request_id": {"type": "string", "description": "幂等键（必填）"},
                "changes": {"type": "array", "items": _edit_schema(), "description": "动作列表"},
                "layout_mode": {"type": "string", "enum": ["reflow", "local"], "description": "可选：覆盖默认重排模式"},
            },
            ["artifact_id", "base_revision", "request_id", "changes"],
        ),
        function_schema(
            "resume_accept",
            "接受明确候选版本：校验机械门、该版视觉覆盖（全部页面已投递并由你看过）与并发版本，"
            "然后原子更新已接受版本。有 vision 时必须给 visual_notes（看图后的结构化结论）。",
            {
                "artifact_id": {"type": "string"},
                "candidate_revision": {"type": "integer", "description": "要接受的候选版本号"},
                "expected_accepted_revision": {"type": "integer", "description": "调用前的已接受版本（并发保护）"},
                "visual_notes": {"type": "string", "description": "看图结论：版式/重叠/越界/观感问题与判断"},
                "request_id": {"type": "string"},
            },
            ["artifact_id", "candidate_revision", "expected_accepted_revision"],
        ),
        function_schema(
            "resume_restore",
            "回退到历史版本：创建新的可追溯恢复版本（不删除历史、不复用旧 revision）。",
            {
                "artifact_id": {"type": "string"},
                "target_revision": {"type": "integer", "description": "要恢复到的历史版本号"},
                "expected_accepted_revision": {"type": "integer"},
                "request_id": {"type": "string"},
            },
            ["artifact_id", "target_revision", "expected_accepted_revision"],
        ),
        function_schema(
            "resume_preview",
            "读取指定版本的页面预览图（revision 绑定）。当自动返回的图像超出单次预算时，"
            "用它补看剩余页；补看前不得记录视觉通过。",
            {
                "artifact_id": {"type": "string"},
                "revision": {"type": "integer"},
                "pages": {"type": "array", "items": {"type": "integer"}, "description": "页码（1-based）；省略则全部"},
            },
            ["artifact_id", "revision"],
        ),
    ]


def resume_tools() -> list[dict]:
    """resume_pro 的领域工具面：legacy 三入口 + v2 六入口。"""
    return _legacy_tools() + resume_v2_tools()
