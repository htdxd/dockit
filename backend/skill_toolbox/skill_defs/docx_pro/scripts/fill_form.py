"""fill_form.py — generate a fillable Word form (.docx).

Usage: python fill_form.py <form.json> <output.docx>

Form spec schema (all fields optional except title):

{
  "title": "员工入职信息表",
  "intro": "说明文字",
  "fields": [
    {"type": "text",       "label": "姓名",     "alias": "Full Name",  "tag": "full_name", "placeholder": "请输入姓名"},
    {"type": "richtext",   "label": "备注",     "alias": "Notes",      "tag": "notes"},
    {"type": "dropdown",   "label": "部门",     "alias": "Department", "tag": "dept", "items": ["工程部", "财务部", "HR"]},
    {"type": "date",       "label": "入职日期", "alias": "Start Date", "tag": "start_date", "format": "yyyy年MM月dd日"},
    {"type": "checkbox",   "label": "同意条款", "name": "agree_terms", "checked": false},
    {"type": "mergefield", "label": "合同编号", "name": "ContractNo"}
  ],
  "protection": "forms",          // 加文档保护（可选）
  "font": "Microsoft YaHei"       // CJK 表单必设
}

Implements the word-form hard floor: every SDT carries alias+tag; dropdowns
have non-empty items; checkboxes use legacy FormField (name <= 20 chars);
document protection last; no underscore-line placeholders.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from docx_pro_engine import load_json, set_run_fonts, workspace_path


def _add_sdt_paragraph(document: Document, field: dict) -> None:
    """Block-level SDT (text/richtext/dropdown/date) with alias + tag."""
    sdt = OxmlElement("w:sdt")
    sdt_pr = OxmlElement("w:sdtPr")
    alias = OxmlElement("w:alias")
    alias.set(qn("w:val"), str(field.get("alias", field.get("label", ""))))
    sdt_pr.append(alias)
    tag = OxmlElement("w:tag")
    tag.set(qn("w:val"), str(field.get("tag", field.get("label", ""))))
    sdt_pr.append(tag)

    ftype = field.get("type", "text")
    if ftype in ("dropdown", "combobox"):
        drop = OxmlElement("w:dropDownList" if ftype == "dropdown" else "w:comboBox")
        for item in field.get("items", []):
            li = OxmlElement("w:listItem")
            li.set(qn("w:displayText"), str(item))
            li.set(qn("w:value"), str(item))
            drop.append(li)
        sdt_pr.append(drop)
    elif ftype == "date":
        date = OxmlElement("w:date")
        fmt = OxmlElement("w:dateFormat")
        fmt.set(qn("w:val"), str(field.get("format", "yyyy-MM-dd")))
        date.append(fmt)
        sdt_pr.append(date)

    sdt_content = OxmlElement("w:sdtContent")
    paragraph = OxmlElement("w:p")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = str(field.get("placeholder", ""))
    run.append(text)
    paragraph.append(run)
    sdt_content.append(paragraph)
    sdt.append(sdt_pr)
    sdt.append(sdt_content)
    document.paragraphs[-1]._p.addnext(sdt)


def _add_formfield_checkbox(document: Document, field: dict) -> None:
    """Legacy FormField checkbox (the only real checkbox; SDT checkbox is not
    implemented in OOXML tooling). name <= 20 chars per schema MaxLength."""
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    fld_char = OxmlElement("w:fldChar")
    fld_char.set(qn("w:fldCharType"), "begin")
    run._r.append(fld_char)
    ff_data = OxmlElement("w:ffData")
    name = OxmlElement("w:name")
    name.set(qn("w:val"), str(field.get("name", "checkbox"))[:20])
    ff_data.append(name)
    enabled = OxmlElement("w:enabled")
    enabled.set(qn("w:val"), "true")
    ff_data.append(enabled)
    check = OxmlElement("w:checkBox")
    size = OxmlElement("w:sizeAuto")
    check.append(size)
    default = OxmlElement("w:default")
    default.set(qn("w:val"), "1" if field.get("checked") else "0")
    check.append(default)
    ff_data.append(check)
    run._r.append(ff_data)
    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char_end)
    paragraph.add_run(f"  {field.get('label', '')}")


def _add_mergefield(document: Document, field: dict) -> None:
    """MERGEFIELD placeholder for mail-merge templates."""
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    fld_char = OxmlElement("w:fldChar")
    fld_char.set(qn("w:fldCharType"), "begin")
    run._r.append(fld_char)
    instr = paragraph.add_run()
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = f' MERGEFIELD {field.get("name", "Field")} '
    instr._r.append(instr_text)
    run = paragraph.add_run()
    fld_char_sep = OxmlElement("w:fldChar")
    fld_char_sep.set(qn("w:fldCharType"), "separate")
    run._r.append(fld_char_sep)
    run = paragraph.add_run(f"«{field.get('name', 'Field')}»")
    run = paragraph.add_run()
    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char_end)


def _enable_protection(document: Document) -> None:
    settings = document.settings.element
    protection = OxmlElement("w:documentProtection")
    protection.set(qn("w:edit"), "forms")
    protection.set(qn("w:enforcement"), "1")
    settings.append(protection)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: fill_form.py <form.json> <output.docx>", file=sys.stderr)
        sys.exit(2)
    spec_path = workspace_path(sys.argv[1], must_exist=True)
    output_path = workspace_path(sys.argv[2], must_exist=False)
    spec = load_json(spec_path)

    document = Document()
    body_font = spec.get("font", "Microsoft YaHei")
    style = document.styles["Normal"]
    style.font.size = Pt(11)
    style.element.get_or_add_rPr().set(qn("w:eastAsia"), body_font)

    title = document.add_heading(spec.get("title", "表单"), level=0)
    for run in title.runs:
        set_run_fonts(run, body_font, "Calibri")

    if spec.get("intro"):
        intro = document.add_paragraph(spec["intro"])
        set_run_fonts(intro.runs[0], body_font, "Calibri")

    for field in spec.get("fields", []):
        label = field.get("label", "")
        if label and field.get("type") != "checkbox":
            label_p = document.add_paragraph()
            label_run = label_p.add_run(label)
            label_run.font.bold = True
            set_run_fonts(label_run, body_font, "Calibri")
        ftype = field.get("type", "text")
        if ftype == "checkbox":
            _add_formfield_checkbox(document, field)
        elif ftype == "mergefield":
            _add_mergefield(document, field)
        else:
            _add_sdt_paragraph(document, field)

    if spec.get("protection") == "forms":
        _enable_protection(document)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    print(json.dumps({
        "ok": True,
        "output": str(output_path),
        "fields": len(spec.get("fields", [])),
        "protection": bool(spec.get("protection") == "forms"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
