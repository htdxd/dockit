"""apply_template.py — Scenario C (Base-Replace): use a template .docx as the
base and replace its body content with generated content, preserving the
template's sections, headers/footers, and styles.

Usage: python apply_template.py <content.json> <template.docx> <output.docx>

content.json schema: {"title": ..., "sections": [{heading, level, paragraphs}]}

Implementation notes (fused rules):
- Copy the template file to the output first; never mutate the template.
- Preserve styles/sections/headers/footers by construction (we keep the whole
  document and only swap the body paragraphs).
- Strip direct run formatting from injected content so template styles win
  (no format contamination).
- The paragraph count of injected content equals what we write; we do not
  insert empty padding paragraphs.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt

from docx_pro_engine import (
    load_json,
    set_first_line_indent_chars,
    set_line_spacing_twips,
    set_run_fonts,
    workspace_path,
)


def _clear_body_content(document: Document) -> None:
    """Remove all body paragraphs/tables except the final section properties."""
    body = document.element.body
    for child in list(body):
        if child.tag == qn("w:sectPr"):
            continue
        body.remove(child)


def _strip_direct_formatting(paragraph: object) -> None:
    """Remove rPr/pPr that would fight the template styles (format contamination)."""
    # Keep it conservative: only strip inline rPr font names/sizes that clash.
    for r in paragraph._p.findall(qn("w:r")):
        r_pr = r.find(qn("w:rPr"))
        if r_pr is not None:
            for tag in (qn("w:rFonts"), qn("w:sz"), qn("w:szCs")):
                node = r_pr.find(tag)
                if node is not None:
                    r_pr.remove(node)


def _inject_content(document: Document, spec: dict) -> None:
    title = document.add_heading(spec.get("title", "文档"), level=0)
    for run in title.runs:
        set_run_fonts(run, "Microsoft YaHei", "Calibri")
    for section_spec in spec.get("sections", []):
        heading = document.add_heading(section_spec.get("heading", ""), level=int(section_spec.get("level", 1)))
        for run in heading.runs:
            set_run_fonts(run, "SimHei", "Calibri")
        for p_spec in section_spec.get("paragraphs", []):
            paragraph = document.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            set_line_spacing_twips(paragraph, 312)
            set_first_line_indent_chars(paragraph, 200)
            run = paragraph.add_run(p_spec.get("text", ""))
            set_run_fonts(run, "SimSun", "Times New Roman")
            _strip_direct_formatting(paragraph)


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: apply_template.py <content.json> <template.docx> <output.docx>", file=sys.stderr)
        sys.exit(2)
    spec_path = workspace_path(sys.argv[1], must_exist=True)
    template_path = workspace_path(sys.argv[2], must_exist=True)
    output_path = workspace_path(sys.argv[3], must_exist=False)
    spec = load_json(spec_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_path, output_path)
    document = Document(output_path)
    _clear_body_content(document)
    _inject_content(document, spec)
    document.save(output_path)
    print(json.dumps({
        "ok": True,
        "output": str(output_path),
        "template": str(template_path),
        "sections_injected": len(spec.get("sections", [])),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
