"""inject_toc.py — inject a live TOC field + refresh hint + updateFields.

Usage: python inject_toc.py <input.docx> <output.docx>

Implements the fused TOC rules:
- TOC field injected after the last existing paragraph of the first body
  section (appends to body — callers should run this on the raw output of
  build_docx.py before postcheck).
- Mandatory PageBreak immediately after the TOC field.
- Italic gray "right-click → Update Field" hint between TOC and PageBreak.
- settings.xml gets updateFields=true so Word recomputes TOC page numbers on
  open (WPS/Word ignore cached values until then).
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from docx_pro_engine import workspace_path

TOC_INSTR = r'TOC \o "1-3" \h \z \u'


def _inject_toc_field(document: Document) -> None:
    """Append a TOC field paragraph + refresh hint + PageBreak to the body."""
    # TOC title must NOT use a Heading style (prevents TOC self-indexing).
    title = document.add_paragraph()
    title.alignment = 1  # CENTER
    title_run = title.add_run("目录")
    title_run.font.size = Pt(16)
    title_run.font.bold = True

    toc_paragraph = document.add_paragraph()
    run = toc_paragraph.add_run()
    fld_char = OxmlElement("w:fldChar")
    fld_char.set(qn("w:fldCharType"), "begin")
    run._r.append(fld_char)

    instr_paragraph = document.add_paragraph()
    instr_run = instr_paragraph.add_run()
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = TOC_INSTR
    instr_run._r.append(instr_text)

    separate_paragraph = document.add_paragraph()
    sep_run = separate_paragraph.add_run()
    sep_char = OxmlElement("w:fldChar")
    sep_char.set(qn("w:fldCharType"), "separate")
    sep_run._r.append(sep_char)

    end_paragraph = document.add_paragraph()
    end_run = end_paragraph.add_run()
    end_char = OxmlElement("w:fldChar")
    end_char.set(qn("w:fldCharType"), "end")
    end_run._r.append(end_char)

    # Refresh hint (italic gray) between TOC and the mandatory PageBreak.
    hint = document.add_paragraph()
    hint.alignment = 1
    hint_run = hint.add_run("（请在 Word 中右键点击目录 → 更新域，以刷新页码）")
    hint_run.font.size = Pt(9)
    hint_run.font.italic = True
    hint_run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    # Mandatory PageBreak after TOC.
    break_paragraph = document.add_paragraph()
    break_run = break_paragraph.add_run()
    break_run.add_break(WD_BREAK.PAGE)


def _enable_update_fields(docx_path: Path) -> None:
    """Patch word/settings.xml in place (rewrite the whole zip) so Word
    recomputes fields (TOC page numbers) on open."""
    from io import BytesIO

    with zipfile.ZipFile(docx_path, "r") as archive:
        names = archive.namelist()
        settings_name = next((n for n in names if n == "word/settings.xml"), None)
        if settings_name is not None:
            settings = archive.read(settings_name).decode("utf-8")
            if "updateFields" not in settings:
                settings = settings.replace(
                    "</w:settings>",
                    '<w:updateFields w:val="true"/></w:settings>',
                    1,
                )
        else:
            settings = None
    if settings is None:
        # No settings part — create one (rare; python-docx always emits it).
        settings = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:updateFields w:val="true"/></w:settings>'
        )
        settings_name = "word/settings.xml"

    buffer = BytesIO()
    with zipfile.ZipFile(docx_path, "r") as src, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename == settings_name:
                dst.writestr(item, settings)
            else:
                dst.writestr(item, src.read(item.filename))
    buffer.seek(0)
    docx_path.write_bytes(buffer.read())


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: inject_toc.py <input.docx> <output.docx>", file=sys.stderr)
        sys.exit(2)
    source = workspace_path(sys.argv[1], must_exist=True)
    output = workspace_path(sys.argv[2], must_exist=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    if source != output:
        shutil.copy2(source, output)

    document = Document(output)
    _inject_toc_field(document)
    document.save(output)
    _enable_update_fields(output)
    print('{"ok": true, "toc_injected": true}')


if __name__ == "__main__":
    main()
