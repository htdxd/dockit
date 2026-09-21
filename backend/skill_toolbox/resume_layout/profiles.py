"""已组件化模板的内部资源、字体基准与几何；不暴露 OOXML 给模型。"""
from dataclasses import dataclass

from skill_toolbox.resume_layout.layout import LayoutGeometry

SUPPORTED_TEMPLATES = frozenset({"t001", "t002", "t003", "t015", "t024", "t109"})


@dataclass(frozen=True)
class TemplateProfile:
    template_id: str
    geometry: LayoutGeometry
    body_width_pt: float
    body_x_pt: float
    photo_part: str
    header_labels: dict[str, str]
    header_slot_order: tuple[str, str, str] = ("date", "org", "role")
    base_font_pt: float = 10.5
    line_pitch_pt: float = 18.0


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
            base_font_pt=10.0,
        )
    from skill_toolbox.resume_layout.component_template import TEMPLATE_IDS, get_spec
    if template_id in TEMPLATE_IDS:
        spec = get_spec(template_id)
        region = spec['regions']['main']
        return TemplateProfile(
            template_id, LayoutGeometry(
                page_top_pt=region['top_pt'], page_bottom_pt=region['bottom_pt'],
                continuation_page_top_pt=region['continuation_top_pt'],
                title_height_pt=20, body_offset_pt=30, text_top_pt=4,
                body_pad_pt=4, title_text_top_pt=spec['title_text_top_pt'],
            ), region['width_pt'], region['x_pt'], spec['photo']['part'],
            spec['header_labels'], base_font_pt=spec['base_font_pt'],
            line_pitch_pt=spec['line_pitch_pt'],
        )
    raise ValueError(f"未适配的组件模板: {template_id}")
