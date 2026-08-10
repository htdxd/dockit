"""build_docx.py — generate a professional .docx from a spec JSON.

Usage: python build_docx.py <spec.json> <output.docx>

Reads the spec schema in ../references/spec-schema.md (relative to the skill
dir). Routes by ``complexity``: simple / standard / academic / gongwen.
Cover, tables, images, OMML formulas, CJK typography, and 3-section page
numbering are built through the shared docx_pro_engine helpers so every fix
(cover budget, WPS tblGrid, CLEAR shading, PAGE \\* arabic) applies uniformly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, Twips

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docx_pro_engine import (
    GONGWEN_FONTS,
    add_footer_page_field,
    add_formula,
    add_image,
    add_section_new_page,
    add_table,
    build_cover,
    build_gongwen_header,
    configure_cjk_doc_defaults,
    ensure_heading_outline_levels,
    load_json,
    set_first_line_indent_chars,
    set_line_spacing_twips,
    set_run_fonts,
    set_section_pgnum_start,
    strip_empty_pgnum,
    workspace_path,
)

PALETTES = {
    "corporate": {
        "title": "1F4E79",
        "table_header": "DCE6F1",
        "accent": "C00000",
        "text_on_dark": "FFFFFF",
        "title_font": "Microsoft YaHei",
    },
    "academic": {
        "title": "000000",
        "table_header": "E7E6E6",
        "accent": "000000",
        "text_on_dark": "FFFFFF",
        "title_font": "SimHei",
    },
    "gongwen": {
        "title": "000000",
        "table_header": "EFEFEF",
        "accent": "FF0000",
        "text_on_dark": "FFFFFF",
        "title_font": "FZXiaoBiaoSong-B05S",
    },
    "minimal": {
        "title": "1F4E79",
        "table_header": "EDEDED",
        "accent": "1F4E79",
        "text_on_dark": "FFFFFF",
        "title_font": "Microsoft YaHei",
    },
}


def _scene_palette(spec: dict[str, Any]) -> dict[str, str]:
    scene = spec.get("scene", "corporate")
    if spec.get("complexity") == "gongwen":
        return PALETTES["gongwen"]
    return PALETTES.get(scene, PALETTES["corporate"])


def _style_body(document: Document) -> None:
    """Configure the Normal style as the docx_pro body default."""
    style = document.styles["Normal"]
    style.font.size = Pt(11)
    style.font.name = "Calibri"
    style.element.get_or_add_rPr().set(qn("w:eastAsia"), "SimSun")
    style.paragraph_format.line_spacing = Twips(312)
    style.paragraph_format.space_after = Pt(6)


def _add_heading(document: Document, text: str, level: int) -> None:
    heading = document.add_heading(text, level=level)
    # CJK heading font via run-level rFonts (python-docx Heading styles default to Latin fonts).
    for run in heading.runs:
        set_run_fonts(run, "SimHei" if level == 1 else "Microsoft YaHei", "Calibri")
    heading.paragraph_format.space_before = Pt(12)
    heading.paragraph_format.space_after = Pt(6)


def _add_body_paragraph(document: Document, paragraph_spec: dict[str, Any], palette: dict[str, str]) -> None:
    style = paragraph_spec.get("style", "body")
    text = paragraph_spec.get("text", "")
    if style == "heading":
        _add_heading(document, text, int(paragraph_spec.get("level", 1)))
        return
    paragraph = document.add_paragraph()
    if style in ("quote", "center"):
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if style == "code":
        from docx_pro_engine import set_paragraph_shading

        paragraph.paragraph_format.left_indent = Twips(360)
        paragraph.paragraph_format.right_indent = Twips(360)
        paragraph.paragraph_format.space_before = Pt(6)
        paragraph.paragraph_format.space_after = Pt(6)
        paragraph.paragraph_format.line_spacing = Twips(240)
        run = paragraph.add_run(text)
        run.font.name = "Consolas"
        run.font.size = Pt(9)
        set_paragraph_shading(paragraph, "F3F4F6")
        return
    set_line_spacing_twips(paragraph, 312)
    if style == "body" and text:
        set_first_line_indent_chars(paragraph, 200)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    run = paragraph.add_run(text)
    run.bold = bool(paragraph_spec.get("bold", False))
    set_run_fonts(run, "SimSun", "Times New Roman")
    if paragraph_spec.get("color"):
        from docx_pro_engine import _set_run_color

        _set_run_color(run, paragraph_spec["color"])


def _add_resource_blocks(
    document: Document, spec: dict[str, Any], palette: dict[str, str]
) -> None:
    """顺序 block 规格（阶段 4）：把 tables/images/formulas 的全局尾部数组
    改为可出现在 section/段落之间的顺序块。

    兼容旧尾部数组（sections + tables/images/formulas 全局追加）：若 spec 无
    `blocks`，仍按旧语义先 section 内容、再全局尾部资源。新规格：
      blocks: [
        {"type": "heading"|"paragraph"|"table"|"image"|"formula", ...}
      ]
    图片/表格/公式可在任意位置插入（如"某段落后插一张图"），由本函数按
    顺序 dispatch 到现有 engine helper，不新建第二套 DOCX 引擎。
    """
    from docx_pro_engine import add_caption

    for block_spec in spec.get("blocks", []):
        block_type = block_spec.get("type", "")
        if block_type == "heading":
            _add_heading(document, block_spec.get("text", ""), int(block_spec.get("level", 1)))
        elif block_type == "paragraph":
            _add_body_paragraph(document, block_spec, palette)
        elif block_type == "table":
            add_table(document, block_spec, palette)
            if block_spec.get("caption"):
                add_caption(document, block_spec["caption"])
        elif block_type == "image":
            add_image(document, block_spec, Path.cwd(), block_spec.get("caption", ""))
        elif block_type == "formula":
            add_formula(document, block_spec.get("latex", ""), block_spec.get("caption", ""))
        else:
            raise ValueError(f"Unknown block type: {block_type}")


def _build_simple(document: Document, spec: dict[str, Any]) -> None:
    """complexity=simple: title + body only, no cover/TOC/pagination."""
    _style_body(document)
    title = document.add_heading(spec.get("title", "文档"), level=0)
    for run in title.runs:
        set_run_fonts(run, "Microsoft YaHei", "Calibri")
    if spec.get("blocks"):
        _add_resource_blocks(document, spec, _scene_palette(spec))
        return
    for section_spec in spec.get("sections", []):
        _add_heading(document, section_spec.get("heading", ""), int(section_spec.get("level", 1)))
        for paragraph_spec in section_spec.get("paragraphs", []):
            _add_body_paragraph(document, paragraph_spec, _scene_palette(spec))
    for table_spec in spec.get("tables", []):
        add_table(document, table_spec, _scene_palette(spec))
        if table_spec.get("caption"):
            from docx_pro_engine import add_caption

            add_caption(document, table_spec["caption"])
    for image_spec in spec.get("images", []):
        add_image(document, image_spec, Path.cwd(), image_spec.get("caption", ""))
    for formula_spec in spec.get("formulas", []):
        add_formula(document, formula_spec.get("latex", ""), formula_spec.get("caption", ""))


def _build_standard(document: Document, spec: dict[str, Any]) -> None:
    """complexity=standard: cover section + body section + footer page numbers."""
    _style_body(document)
    configure_cjk_doc_defaults(document)
    ensure_heading_outline_levels(document)

    # --- Cover section (margins 0, no page number) ---
    cover_section = document.sections[-1]
    palette = _scene_palette(spec)
    build_cover(document, cover_section, spec, palette)
    strip_empty_pgnum(cover_section)

    # --- Body section (NEXT_PAGE, arabic start=1) ---
    body_section = add_section_new_page(document)
    set_section_pgnum_start(body_section, start=1, fmt="decimal")
    add_footer_page_field(document, body_section, fmt="arabic")

    if spec.get("blocks"):
        _add_resource_blocks(document, spec, palette)
        return
    for section_spec in spec.get("sections", []):
        _add_heading(document, section_spec.get("heading", ""), int(section_spec.get("level", 1)))
        for paragraph_spec in section_spec.get("paragraphs", []):
            _add_body_paragraph(document, paragraph_spec, palette)

    for table_spec in spec.get("tables", []):
        add_table(document, table_spec, palette)
        if table_spec.get("caption"):
            from docx_pro_engine import add_caption

            add_caption(document, table_spec["caption"])
    for image_spec in spec.get("images", []):
        add_image(document, image_spec, Path.cwd(), image_spec.get("caption", ""))
    for formula_spec in spec.get("formulas", []):
        add_formula(document, formula_spec.get("latex", ""), formula_spec.get("caption", ""))


def _build_gongwen(document: Document, spec: dict[str, Any]) -> None:
    """GB/T 9704 公文: red header + fixed fonts + -X- page numbers."""
    _style_body(document)
    configure_cjk_doc_defaults(document)
    section = document.sections[-1]
    section.top_margin = Twips(2098)
    section.bottom_margin = Twips(1984)
    section.left_margin = Twips(1588)
    section.right_margin = Twips(1474)

    build_gongwen_header(document, spec.get("org", "XX 机关"))

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.line_spacing = Twips(560)
    run = title.add_run(spec.get("title", ""))
    run.font.size = Pt(22)
    run.font.bold = True
    set_run_fonts(run, GONGWEN_FONTS["title"], "SimHei")

    if spec.get("wenhao"):
        wenhao = document.add_paragraph()
        wenhao.alignment = WD_ALIGN_PARAGRAPH.CENTER
        wenhao_run = wenhao.add_run(spec["wenhao"])
        set_run_fonts(wenhao_run, GONGWEN_FONTS["body"])
        wenhao_run.font.size = Pt(16)

    body_index = 0
    for section_spec in spec.get("sections", []):
        heading = section_spec.get("heading", "")
        level = int(section_spec.get("level", 1))
        if heading:
            h = document.add_paragraph()
            h.paragraph_format.line_spacing = Twips(560)
            h_run = h.add_run(heading)
            h_run.font.size = Pt(16)
            if level == 1:
                set_run_fonts(h_run, GONGWEN_FONTS["h1"])
            elif level == 2:
                set_run_fonts(h_run, GONGWEN_FONTS["h2"])
                set_first_line_indent_chars(h, 200)
            else:
                set_run_fonts(h_run, GONGWEN_FONTS["h3"])
                h_run.font.bold = True
                set_first_line_indent_chars(h, 200)
        for paragraph_spec in section_spec.get("paragraphs", []):
            p = document.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.line_spacing = Twips(560)
            set_first_line_indent_chars(p, 200)
            body_index += 1
            run = p.add_run(paragraph_spec.get("text", ""))
            run.font.size = Pt(16)
            set_run_fonts(run, GONGWEN_FONTS["body"])

    if spec.get("signature"):
        sig = document.add_paragraph()
        sig.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        sig_run = sig.add_run(spec["signature"])
        sig_run.font.size = Pt(16)
        set_run_fonts(sig_run, GONGWEN_FONTS["body"])
    if spec.get("date"):
        date_p = document.add_paragraph()
        date_p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        date_run = date_p.add_run(spec["date"])
        date_run.font.size = Pt(16)
        set_run_fonts(date_run, GONGWEN_FONTS["body"])

    # Footer: -X- 页码 (SimSun 四号)
    footer = section.footer
    footer.is_linked_to_previous = False
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    dash_run = fp.add_run("-")
    dash_run.font.size = Pt(14)
    set_run_fonts(dash_run, GONGWEN_FONTS["note"])
    add_footer_page_field(document, section, fmt="arabic")
    # The helper writes "PAGE " then the field; append trailing dash run.
    last_para = footer.paragraphs[0]
    dash2 = last_para.add_run("-")
    dash2.font.size = Pt(14)
    set_run_fonts(dash2, GONGWEN_FONTS["note"])


def _build_from_spec(document: Document, spec: dict[str, Any]) -> None:
    complexity = spec.get("complexity", "standard")
    if complexity == "simple":
        _build_simple(document, spec)
    elif complexity == "gongwen":
        _build_gongwen(document, spec)
    else:  # standard / academic
        _build_standard(document, spec)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: build_docx.py <spec.json> <output.docx>", file=sys.stderr)
        sys.exit(2)
    spec_path = workspace_path(sys.argv[1], must_exist=True)
    output_path = workspace_path(sys.argv[2], must_exist=False)
    spec = load_json(spec_path)

    document = Document()
    _build_from_spec(document, spec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    print(json.dumps({"ok": True, "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
