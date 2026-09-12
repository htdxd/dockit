"""两套已适配单栏模板的内部资源与几何；不暴露给模型。"""
from dataclasses import dataclass

from skill_toolbox.resume_layout.layout import LayoutGeometry


@dataclass(frozen=True)
class TemplateProfile:
    template_id: str
    geometry: LayoutGeometry
    body_width_pt: float
    body_x_pt: float
    photo_part: str
    header_labels: dict[str, str]
    header_slot_order: tuple[str, str, str] = ("date", "org", "role")


def get_profile(template_id: str) -> TemplateProfile:
    from skill_toolbox.resume_layout.emit import INFO_FIELD_LABELS

    if template_id == "t109":
        return TemplateProfile("t109", LayoutGeometry(), 532.5, 42.0,
                               "word/media/image1.png", dict(INFO_FIELD_LABELS))
    if template_id == "t001":
        return TemplateProfile(
            "t001", LayoutGeometry(
                page_top_pt=243.7, page_bottom_pt=815.0,
                title_height_pt=23.2, body_offset_pt=29.4,
                text_top_pt=4.05, body_pad_pt=4.0, title_text_top_pt=3.9,
                continuation_page_top_pt=110.0,
            ), 506.75, 57.6, "word/media/image1.jpeg",
            {"姓名": "name", "年龄": "age", "学历": "degree", "求职意向": "target_role",
             "手机": "phone", "邮箱": "email", "微信": "wechat", "地址": "address"},
            ("role", "org", "date"),
        )
    raise ValueError(f"未适配的组件模板: {template_id}")
