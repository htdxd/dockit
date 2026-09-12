"""Shared OOXML building blocks for the docx_pro skill scripts.

Everything in this module targets the two failure modes the fused skills
identified as most expensive: Word-vs-WPS rendering divergence, and blank
pages / cover overflow from hand-rolled cover code. Use these helpers instead
of ad-hoc oxml so every generated document inherits the same fixes:

- Cover recipe skeleton (16838-twip EXACT wrapper table, all borders nil,
  computed title size + spacing, explicit color on shaded text).
- CJK font slots (ascii/hAnsi/eastAsia) + firstLineChars=200 indent.
- Real Heading styles with OutlineLevel (TOC source requirement).
- Percentage table column widths (WPS tblGrid bug), CLEAR shading,
  tblHeader/cantSplit/keepNext.
- PIL-driven image aspect-ratio preservation.
- OMML math injection with matplotlib PNG fallback.
- 3-section page numbering with literal field switches (`PAGE \\* arabic`).
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Length, Mm, Pt, RGBColor, Twips
from docx.table import Table
from docx.text.paragraph import Paragraph

# --- unit helpers -------------------------------------------------------------

A4_WIDTH_TWIPS = 11906
A4_HEIGHT_TWIPS = 16838
COVER_HEIGHT_BUDGET_TWIPS = 15638  # 16838 - 1200 safety (Word renders tall fonts bigger)
SAFE_MAX_COVER_FONT_PT = 40
DEFAULT_LINE_312 = 312  # 1.3x for 12pt SimSun body; override per profile

_MATH_COMPLEX_RE = re.compile(r"\\(frac|sum|int|sqrt|matrix|cases|begin)", re.IGNORECASE)


def workspace_path(value: str, must_exist: bool) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cjk_title_layout(title: str, available_twips: int, preferred_pt: float = 40.0) -> float:
    """Compute the largest title size that fits in <=3 lines, floor 24pt.

    CJK glyph width ~= pt (in points); each pt maps to 20 twips of width.
    Latin chars count roughly half-width. Shrink 2pt at a time (per the
    fused cover recipe) until the line estimate fits.
    """
    pt = preferred_pt
    while pt >= 24.0:
        # effective width per char: CJK=1.0em, Latin=0.55em
        width_units = sum(1.0 if ord(ch) > 0x2E7F else 0.55 for ch in title)
        chars_per_line = max(1.0, available_twips / (pt * 20.0))
        lines = math.ceil(width_units / chars_per_line)
        if lines <= 3:
            return pt
        pt -= 2.0
    return 24.0


def _set_cell_margins(cell: Any, top: int = 60, bottom: int = 60, left: int = 120, right: int = 120) -> None:
    """Set cell margins in twips (defaults: ~3pt/6pt). Text touches borders without them."""
    tc_pr = cell._tc.get_or_add_tcPr()
    mar = OxmlElement("w:tcMar")
    for name, val in (("top", top), ("bottom", bottom), ("start", left), ("end", right)):
        node = OxmlElement(f"w:{name}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        mar.append(node)
    tc_pr.append(mar)


def set_cell_shading(cell: Any, fill: str) -> None:
    """CLEAR shading (never SOLID — WPS renders SOLID as solid black)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    tc_pr.append(shading)


def set_cell_borders_none(cell: Any) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "nil")
        borders.append(node)
    tc_pr.append(borders)


def table_all_borders_none(table: Table) -> None:
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "nil")
        borders.append(node)
    tbl_pr.append(borders)


def set_row_height_exact(row: Any, twips: int) -> None:
    row.height = Twips(twips)
    row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
    # Belt-and-braces: ensure the oxml hRule is literally "exact" (some
    # python-docx versions emit "atLeast" from the property setter alone).
    tr_pr = row._tr.get_or_add_trPr()
    height = tr_pr.find(qn("w:trHeight"))
    if height is None:
        height = OxmlElement("w:trHeight")
        tr_pr.append(height)
    height.set(qn("w:val"), str(twips))
    height.set(qn("w:hRule"), "exact")


def set_row_cant_split(row: Any) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def set_row_table_header(row: Any) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_table_column_widths_pct(table: Table, widths_pct: list[float]) -> None:
    """Percentage column widths — WPS breaks DXA tblGrid layouts (all gridCol=100)."""
    total = sum(widths_pct) or len(widths_pct)
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        for col, node in zip(widths_pct, grid.findall(qn("w:gridCol")), strict=False):
            node.set(qn("w:w"), str(int(round(col / total * 100))))
    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    for row in table.rows:
        cells = row.cells
        for idx, cell in enumerate(cells):
            pct = widths_pct[idx] / total * 100.0 if idx < len(widths_pct) else 100.0 / total
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = OxmlElement("w:tcW")
            tc_w.set(qn("w:w"), str(int(round(pct))))
            tc_w.set(qn("w:type"), "pct")
            tc_pr.append(tc_w)


def set_line_spacing_twips(paragraph: Paragraph, twips: int = DEFAULT_LINE_312) -> None:
    paragraph.paragraph_format.line_spacing = Twips(twips)
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY


def set_visual_paragraph_layout(
    paragraph: Paragraph, *, keep_with_next: bool = False
) -> None:
    """Give inline media a line box that can grow to its rendered height.

    Body paragraphs use exact CJK line spacing for predictable text layout,
    but that rule clips tall inline drawings to the line height. Media must
    use an at-least line box and remain an isolated paragraph.
    """
    fmt = paragraph.paragraph_format
    fmt.line_spacing = 1.0
    fmt.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
    fmt.space_before = Pt(6)
    fmt.space_after = Pt(6)
    fmt.keep_together = True
    fmt.keep_with_next = keep_with_next


def set_first_line_indent_chars(paragraph: Paragraph, chars: int = 200) -> None:
    """2-char CJK first-line indent via firstLineChars (scales with font size)."""
    p_pr = paragraph._p.get_or_add_pPr()
    ind = p_pr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        p_pr.append(ind)
    ind.set(qn("w:firstLineChars"), str(chars))


def set_run_fonts(run: Any, east_asia: str, ascii_font: str | None = None) -> None:
    """Set the CJK + Latin font slots on a run."""
    ascii_font = ascii_font or east_asia
    r_pr = run._r.get_or_add_rPr()
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), ascii_font)
    fonts.set(qn("w:hAnsi"), ascii_font)
    fonts.set(qn("w:eastAsia"), east_asia)
    fonts.set(qn("w:cs"), "Arial")
    r_pr.append(fonts)


def set_paragraph_shading(paragraph: Paragraph, fill: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    p_pr.append(shading)


def set_paragraph_border_bottom(paragraph: Paragraph, color: str = "C00000", sz: str = "8") -> None:
    """Horizontal rule via paragraph bottom border (never text dashes)."""
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), sz)
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    p_bdr.append(bottom)
    p_pr.append(p_bdr)


# --- styles -------------------------------------------------------------------

def ensure_heading_outline_levels(document: Document) -> None:
    """Heading styles must carry outlineLvl (H1=0, H2=1, H3=2) or Word's TOC
    update finds nothing. python-docx Heading styles already set outlineLvl in
    their style definitions, but enforce it explicitly."""
    for style_name, level in (("Heading 1", 0), ("Heading 2", 1), ("Heading 3", 2)):
        style = document.styles[style_name]
        p_pr = style.element.get_or_add_pPr()
        if p_pr.find(qn("w:outlineLvl")) is None:
            outline = OxmlElement("w:outlineLvl")
            outline.set(qn("w:val"), str(level))
            p_pr.append(outline)


def configure_cjk_doc_defaults(document: Document) -> None:
    """Set the four font slots + lang on docDefaults so CJK resolves correctly."""
    styles_element = document.styles.element
    doc_defaults = styles_element.find(qn("w:docDefaults"))
    if doc_defaults is None:
        doc_defaults = OxmlElement("w:docDefaults")
        styles_element.insert(0, doc_defaults)
    r_pr_default = doc_defaults.find(qn("w:rPrDefault"))
    if r_pr_default is None:
        r_pr_default = OxmlElement("w:rPrDefault")
        doc_defaults.append(r_pr_default)
    r_pr = r_pr_default.find(qn("w:rPr"))
    if r_pr is None:
        r_pr = OxmlElement("w:rPr")
        r_pr_default.append(r_pr)
    fonts = r_pr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, fonts)
    fonts.set(qn("w:ascii"), "Calibri")
    fonts.set(qn("w:hAnsi"), "Calibri")
    fonts.set(qn("w:eastAsia"), "SimSun")
    fonts.set(qn("w:cs"), "Arial")
    lang = r_pr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        r_pr.append(lang)
    lang.set(qn("w:val"), "en-US")
    lang.set(qn("w:eastAsia"), "zh-CN")


# --- cover --------------------------------------------------------------------

def _hex_rgb(value: str) -> RGBColor:
    """Accept '#RRGGBB' or 'RRGGBB'; raise on malformed input."""
    clean = value.lstrip("#")
    if len(clean) != 6 or not all(ch in "0123456789abcdefABCDEF" for ch in clean):
        raise ValueError(f"Invalid hex color: {value!r}")
    return RGBColor(int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))


def _set_run_color(run: Any, value: str) -> None:
    if isinstance(value, str):
        run.font.color.rgb = _hex_rgb(value)
    else:
        run.font.color.rgb = value


def build_cover(
    document: Document,
    section: Any,
    spec: dict[str, Any],
    palette: dict[str, str],
) -> None:
    """R1/R2 cover skeleton per the fused cover recipe."""
    title = spec.get("title", "文档标题")
    subtitle = spec.get("subtitle") or spec.get("cover", {}).get("subtitle", "")
    meta_lines = spec.get("cover", {}).get("metaLines", []) or [
        spec.get("author", ""),
        spec.get("date", ""),
    ]
    dark = bool(spec.get("cover", {}).get("dark", False))

    # 1. Cover section margins = 0 (wrapper must touch page edges).
    section.top_margin = 0
    section.bottom_margin = 0
    section.left_margin = 0
    section.right_margin = 0

    # 2. Wrapper table: 1x1 exact 16838, all borders nil.
    wrapper = document.add_table(rows=1, cols=1)
    table_all_borders_none(wrapper)
    set_cell_borders_none(wrapper.cell(0, 0))
    set_row_height_exact(wrapper.rows[0], A4_HEIGHT_TWIPS)
    cell = wrapper.cell(0, 0)
    _set_cell_margins(cell, top=0, bottom=0, left=1134, right=1134)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP

    # 3. Title size via calcTitleLayout (never hardcoded above 40pt).
    available = A4_WIDTH_TWIPS - 2 * 1134
    title_pt = cjk_title_layout(title, available, preferred_pt=SAFE_MAX_COVER_FONT_PT)
    title_line_twips = max(276, math.ceil(title_pt * 23))

    # 4. Vertical budget: reserve title lines + meta + footer safety.
    used = title_line_twips * 2 + 800  # title block + meta block + footer reserve
    spacing_before = max(800, (COVER_HEIGHT_BUDGET_TWIPS - used) // 3)

    paragraph = cell.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Twips(spacing_before)
    paragraph.paragraph_format.line_spacing = Twips(title_line_twips)
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
    run = paragraph.add_run(title)
    run.font.size = Pt(title_pt)
    run.font.bold = True
    color = palette.get("text_on_dark", "FFFFFF") if dark else palette.get("title", "1F4E79")
    _set_run_color(run, color)
    set_run_fonts(run, palette.get("title_font", "Microsoft YaHei"), "Calibri")

    if subtitle:
        sub = cell.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub.paragraph_format.space_before = Twips(max(276, math.ceil(16 * 23)))
        sub.paragraph_format.line_spacing = Twips(max(276, math.ceil(16 * 23)))
        sub_run = sub.add_run(subtitle)
        sub_run.font.size = Pt(16)
        _set_run_color(sub_run, color)
        set_run_fonts(sub_run, palette.get("title_font", "Microsoft YaHei"), "Calibri")

    for line in meta_lines:
        if not line:
            continue
        meta = cell.add_paragraph()
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta.paragraph_format.space_before = Twips(max(240, math.ceil(12 * 23)))
        meta_run = meta.add_run(line)
        meta_run.font.size = Pt(12)
        _set_run_color(meta_run, color)
        set_run_fonts(meta_run, palette.get("title_font", "Microsoft YaHei"), "Calibri")


# --- sections + page numbers --------------------------------------------------

def add_footer_page_field(document: Document, section: Any, fmt: str) -> None:
    """Live PAGE field with an explicit format switch (WPS ignores pgNumType fmt).

    ``fmt`` must be a Word field switch: "arabic" or "ROMAN" — never "decimal".
    """
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("PAGE ")
    fld_char_begin = OxmlElement("w:fldChar")
    fld_char_begin.set(qn("w:fldCharType"), "begin")
    run._r.append(fld_char_begin)
    run = paragraph.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" PAGE \\* {fmt} "
    run._r.append(instr)
    run = paragraph.add_run()
    fld_char_sep = OxmlElement("w:fldChar")
    fld_char_sep.set(qn("w:fldCharType"), "separate")
    run._r.append(fld_char_sep)
    run = paragraph.add_run("1")
    run = paragraph.add_run()
    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char_end)


def set_section_pgnum_start(section: Any, start: int | None = None, fmt: str | None = None) -> None:
    """w:pgNumType start/fmt on a section (Word pagination control)."""
    sect_pr = section._sectPr
    pg_num = sect_pr.find(qn("w:pgNumType"))
    if pg_num is None:
        pg_num = OxmlElement("w:pgNumType")
        sect_pr.append(pg_num)
    if start is not None:
        pg_num.set(qn("w:start"), str(start))
    if fmt is not None:
        pg_num.set(qn("w:fmt"), fmt)


def strip_empty_pgnum(section: Any) -> None:
    """Remove a pgNumType element that has no attributes (WPS confusion)."""
    sect_pr = section._sectPr
    pg_num = sect_pr.find(qn("w:pgNumType"))
    if pg_num is not None and not pg_num.attrib:
        sect_pr.remove(pg_num)


def add_section_new_page(document: Document) -> Any:
    section = document.add_section(WD_SECTION.NEW_PAGE)
    section.top_margin = Twips(1440 * 0.75)
    section.bottom_margin = Twips(1440 * 0.75)
    section.left_margin = Twips(1440 * 0.75)
    section.right_margin = Twips(1440 * 0.75)
    return section


# --- tables -------------------------------------------------------------------

def add_table(
    document: Document,
    spec: dict[str, Any],
    palette: dict[str, str],
    body_east_asia: str = "SimSun",
) -> Table | None:
    headers = spec.get("headers")
    rows = spec.get("rows")
    if not headers or not rows:
        return None
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = False

    widths = spec.get("widths_pct") or [100.0 / len(headers)] * len(headers)
    set_table_column_widths_pct(table, widths)

    header_cells = table.rows[0].cells
    for idx, header in enumerate(headers):
        _set_cell_margins(header_cells[idx])
        set_cell_shading(header_cells[idx], palette.get("table_header", "DCE6F1"))
        paragraph = header_cells[idx].paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(str(header))
        run.font.bold = True
        set_run_fonts(run, body_east_asia, "Times New Roman")
    set_row_table_header(table.rows[0])
    set_row_cant_split(table.rows[0])

    for row_spec in rows:
        cells = table.add_row().cells
        for idx, value in enumerate(row_spec):
            if idx >= len(headers):
                break
            _set_cell_margins(cells[idx])
            paragraph = cells[idx].paragraphs[0]
            run = paragraph.add_run(str(value))
            set_run_fonts(run, body_east_asia, "Times New Roman")
        set_row_cant_split(table.rows[-1])
    return table


def add_caption(document: Document, text: str, *, italic: bool = False) -> None:
    """图注/表注: centered, 五号 (10.5pt), SimSun/Times."""
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_visual_paragraph_layout(paragraph)
    run = paragraph.add_run(text)
    run.font.size = Pt(10.5)
    run.font.italic = italic
    set_run_fonts(run, "SimSun", "Times New Roman")


# --- images -------------------------------------------------------------------

def add_image(
    document: Document,
    spec: dict[str, Any],
    source_root: Path,
    caption: str = "",
) -> None:
    """Insert an image preserving aspect ratio (PIL reads pixel dims; we set
    only width and let height follow). Never hardcode both width and height."""
    source = spec.get("source", "")
    width_mm = float(spec.get("width_mm", 0) or 0)
    if not source:
        return
    image_path = workspace_path(source, must_exist=True) if Path(source).is_absolute() else source_root / source
    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {source}")
    try:
        from PIL import Image as PILImage

        with PILImage.open(image_path) as img:
            width_px, height_px = img.size
    except Exception:  # noqa: BLE001 - PIL absent or unreadable; fall back to docx default
        width_px, height_px = 0, 0

    if width_mm <= 0:
        section = document.sections[-1]
        # python-docx 的 section 尺寸是 Twips（Length 子类），相减会退化成裸
        # int，直接 .mm 会崩（'int' object has no attribute 'mm'）。差值仍是
        # EMU，用 Length 重建单位再换算 mm。
        usable = section.page_width - section.left_margin - section.right_margin
        width_mm = Length(usable).mm * 0.9
    else:
        # 显式 width_mm 超过可用页宽时自动收窄（防 postcheck image-overflow
        # 反复触发、模型盲改宽度烧步）。图片按比例缩放，观感不受影响。
        section = document.sections[-1]
        usable = section.page_width - section.left_margin - section.right_margin
        usable_mm = Length(usable).mm
        width_mm = min(width_mm, usable_mm * 0.95)
    if width_px and height_px:
        height_mm = width_mm * (height_px / width_px)
        # 高度钳制：超高 aspect 长图（如 684×2486）宽度铺满后高度会超过
        # 页面（画出页外、文字与图片重叠）。按可用页高反算宽度，保比例缩。
        section = document.sections[-1]
        usable_h_mm = Length(section.page_height - section.top_margin - section.bottom_margin).mm
        if height_mm > usable_h_mm * 0.95:
            width_mm = (usable_h_mm * 0.95) * (width_px / height_px)
            height_mm = usable_h_mm * 0.95
    else:
        height_mm = width_mm

    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_visual_paragraph_layout(paragraph, keep_with_next=bool(caption))
    run = paragraph.add_run()
    run.add_picture(str(image_path), width=Mm(width_mm), height=Mm(height_mm))
    if caption:
        add_caption(document, caption, italic=True)


# --- math ---------------------------------------------------------------------

def add_formula(document: Document, formula: str, caption: str = "") -> None:
    """OMML math object for simple LaTeX-ish formulas; matplotlib PNG fallback
    for complex ones (3+ nesting / matrices / piecewise)."""
    if _MATH_COMPLEX_RE.search(formula):
        _add_formula_image(document, formula, caption)
        return
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    math_para = OxmlElement("m:oMathPara")
    math = OxmlElement("m:oMath")
    run = OxmlElement("m:r")
    text = OxmlElement("m:t")
    text.text = _latex_to_plain(formula)
    run.append(text)
    math.append(run)
    math_para.append(math)
    paragraph._p.append(math_para)
    if caption:
        add_caption(document, caption)


def _latex_to_plain(latex: str) -> str:
    return latex.replace("^", "superscript").replace("_", "subscript")[:200]


def _add_formula_image(document: Document, formula: str, caption: str) -> None:
    """matplotlib fallback for complex formulas — renders LaTeX to a PNG."""
    import subprocess
    import tempfile

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        # No matplotlib: emit the raw LaTeX as text so nothing is lost silently.
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(formula)
        run.font.italic = True
        set_run_fonts(run, "SimSun", "Times New Roman")
        return
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "formula.png"
        code = (
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            f"fig = plt.figure(figsize=(6, 0.8))\n"
            f"fig.text(0.5, 0.5, r'{formula}', ha='center', va='center', fontsize=16)\n"
            f"fig.savefig({str(png)!r}, dpi=200, bbox_inches='tight', transparent=True)\n"
        )
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=60)
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_visual_paragraph_layout(paragraph, keep_with_next=bool(caption))
        run = paragraph.add_run()
        run.add_picture(str(png), width=Mm(90))
    if caption:
        add_caption(document, caption)


# --- gongwen (GB/T 9704) ------------------------------------------------------

GONGWEN_FONTS = {
    "title": "FZXiaoBiaoSong-B05S",
    "h1": "SimHei",
    "h2": "KaiTi_GB2312",
    "h3": "FangSong_GB2312",
    "body": "FangSong_GB2312",
    "note": "SimSun",
}


def build_gongwen_header(document: Document, org_name: str) -> None:
    """红头 + 红线 for GB/T 9704 公文."""
    section = document.sections[-1]
    header = section.header
    header.is_linked_to_previous = False
    paragraph = header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(org_name)
    run.font.size = Pt(22)
    run.font.bold = True
    _set_run_color(run, "FF0000")
    set_run_fonts(run, "FZXiaoBiaoSong-B05S", "SimHei")
    redline = header.add_paragraph()
    set_paragraph_border_bottom(redline, color="FF0000", sz="8")
