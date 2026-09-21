"""由模板定义派生统一的排版配置。"""
from dataclasses import dataclass

from skill_toolbox.resume_layout.component_template import TEMPLATE_IDS, get_spec
from skill_toolbox.resume_layout.layout import LayoutGeometry

SUPPORTED_TEMPLATES = TEMPLATE_IDS


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
    if template_id not in SUPPORTED_TEMPLATES:
        raise ValueError(f"未适配的组件模板: {template_id}")
    spec = get_spec(template_id)
    region = spec["regions"]["main"]
    geometry = {'title_height_pt': 20, 'body_offset_pt': 30, 'text_top_pt': 4, 'body_pad_pt': 4}
    geometry.update(spec.get("geometry", {}))
    return TemplateProfile(
        template_id, LayoutGeometry(
            page_top_pt=region["top_pt"], page_bottom_pt=region["bottom_pt"],
            continuation_page_top_pt=region["continuation_top_pt"],
            title_text_top_pt=spec["title_text_top_pt"], **geometry,
        ), region["width_pt"], region["x_pt"], spec["photo"]["part"],
        spec["header_labels"], tuple(spec.get("header_slot_order", ("date", "org", "role"))),
        spec["base_font_pt"], spec["line_pitch_pt"],
    )
