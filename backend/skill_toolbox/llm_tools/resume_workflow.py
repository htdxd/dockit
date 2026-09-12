"""由同一 Pydantic 契约生成简历工作流工具 schema。"""

from skill_toolbox.contracts.resume_workflow import (
    AcceptRequest, EditRequest, GenerateRequest, PrepareRequest, PreviewRequest,
)

REQUEST_MODELS = {
    "resume_prepare": PrepareRequest,
    "resume_generate": GenerateRequest,
    "resume_edit": EditRequest,
    "resume_preview": PreviewRequest,
    "resume_accept": AcceptRequest,
}

_DESCRIPTIONS = {
    "resume_prepare": "开始简历制作：读取选定材料并查看当前模板支持的栏目与个人信息字段。"
        "material_ids 省略时使用当前任务材料；请根据返回的材料与能力组织内容。",
    "resume_generate": "将准备好的个人信息与栏目条目生成简历候选并返回渲染图。"
        "应先 resume_prepare；target_pages 是允许的最大页数，不强制填满页面。"
        "font_size_pt 是缩放前的正文字号；条目可单独指定字号和 scale，栏目 scale 含标题图标但不改个人信息或照片。"
        "缩放后字号不得小于 8pt 且组件须放得下页面，不能保证所有组件都能放大到 1.25。"
        "条目 id 可省略；照片必须使用材料中的 asset_id。失败时按错误提示修改内容。",
    "resume_edit": "基于最新 candidate_id 批量编辑内容并返回新候选及渲染图。"
        "target_id/section_id/before_id/after_id 使用准备或候选结果给出的真实实例 ID。"
        "update_entry 仅更新显式提供的字段，text=[] 清空正文；插入时省略 after_id 表示追加。"
        "移动条目或栏目必须指定 before_id 或 after_id 之一。"
        "format 按 all/section/entry 范围设置缩放前字号或整体缩放；值是绝对值，不与上次相乘，省略或 null 保持不变。"
        "栏目缩放包含标题图标，不改个人信息或照片；最终字号至少 8pt 且组件必须放得下页面。"
        "target_pages 可单独提高或降低页数上限，无需改写内容；后续只改文字会保留既有字号和缩放。"
        "update_person 修改已有个人字段，空值隐藏标签和值；set_person_field 新增或重命名字段组件，"
        "remove_person_field 删除整个字段组件；replace_photo 的 asset_id 为空时移除照片。"
        "过期候选、未知 ID、非法内容或头部空间不足会被拒绝。",
    "resume_preview": "查看指定候选的渲染图；pages 是从 1 开始的页码，省略时查看全部页。"
        "接受前应检查全部页面的文字、间距和排版；未知候选或无效页码会被拒绝。",
    "resume_accept": "接受已检查的最新候选并交付成果。必须先查看全部页面并填写实际视觉检查结论。"
        "candidate_id 原样使用候选结果中的值；过期候选、机械检查未通过或页面未看完会被拒绝。",
}


def _inline_refs(schema: dict) -> dict:
    """展开当前契约的本地引用，保留判别联合的标准 oneOf/const 校验。"""
    definitions = schema.get("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            definition = definitions[value["$ref"].removeprefix("#/$defs/")]
            value = {**definition, **{key: item for key, item in value.items() if key != "$ref"}}
        # discriminator 的 mapping 仍指向 $defs；oneOf 中的 op.const 足以判别。
        return {key: expand(item) for key, item in value.items() if key not in {"$defs", "discriminator"}}

    return expand(schema)


def workflow_tools() -> list[dict]:
    return [
        {"name": name, "description": _DESCRIPTIONS[name],
         "input_schema": _inline_refs(model.model_json_schema())}
        for name, model in REQUEST_MODELS.items()
    ]
