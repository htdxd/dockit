"""postcheck_docx.py — mechanical quality gate for generated .docx files.

Usage: python postcheck_docx.py <file.docx> [--json] [--strict]

Runs 15 business-rule checks (port of the fused skills' post-generation
checklist; the proprietary plugin only implemented 9 of its advertised 14 —
this implementation covers the full set):

  blank-pages / cover-overflow / line-spacing / image-overflow /
  image-aspect-ratio / font-fallback / heading-levels / shading-type / toc /
  table-margins / table-cross-page / cjk-indent / numbering-continuity /
  cleanliness / content-quality

Exit code: 0 = pass; 2 = at least one error; 1 = warnings only (with --strict).
Use --json for machine-readable output consumed by the skill runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"

W = f"{{{W_NS}}}"
WP = f"{{{WP_NS}}}"
A = f"{{{A_NS}}}"
R = f"{{{R_NS}}}"
PIC = f"{{{PIC_NS}}}"

# Fonts considered safe (matches the fused SAFE list).
SAFE_FONTS = {
    "SimSun", "SimHei", "Microsoft YaHei", "Microsoft YaHei UI", "FangSong",
    "FangSong_GB2312", "KaiTi", "KaiTi_GB2312", "FZXiaoBiaoSong-B05S",
    "Times New Roman", "Arial", "Calibri", "Cambria", "Georgia", "Consolas",
    "Segoe UI", "Calibri Light",
}

# Placeholder / template tokens that must never render as data.
CLEANLINESS_RE = re.compile(
    r"(\$[A-Za-z_][A-Za-z0-9_]*\$|\{\{[^}]+\}\}|<TODO>|lorem|xxxx|TBD|待补)",
    re.IGNORECASE,
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "passed": self.passed, "message": self.message, "severity": self.severity}

    def __str__(self) -> str:
        icon = "✅" if self.passed else ("❌" if self.severity == "error" else "⚠️")
        return f"{icon} [{self.name}] {self.message}"


def read_document_xml(docx_path: str) -> ET.Element:
    with zipfile.ZipFile(docx_path) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def read_rels(docx_path: str) -> dict[str, str]:
    """rId -> target path for image relationships."""
    mapping: dict[str, str] = {}
    try:
        with zipfile.ZipFile(docx_path) as archive:
            rels = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        for rel in rels:
            if rel.get("Type", "").endswith("/image"):
                mapping[rel.get("Id", "")] = rel.get("Target", "")
    except (KeyError, ET.ParseError):
        pass
    return mapping


def read_settings_update_fields(docx_path: str) -> bool:
    try:
        with zipfile.ZipFile(docx_path) as archive:
            settings = archive.read("word/settings.xml").decode("utf-8")
        return "updateFields" in settings
    except KeyError:
        return False


def page_setup(root: ET.Element) -> tuple[int, int, int, int]:
    """page width/height + left/right margins in twips (from first sectPr)."""
    sect_pr = root.find(f".//{W}sectPr")
    pg_sz = sect_pr.find(f"{W}pgSz") if sect_pr is not None else None
    pg_mar = sect_pr.find(f"{W}pgMar") if sect_pr is not None else None
    width = int(pg_sz.get(f"{W}w")) if pg_sz is not None and pg_sz.get(f"{W}w") else 11906
    height = int(pg_sz.get(f"{W}h")) if pg_sz is not None and pg_sz.get(f"{W}h") else 16838
    left = int(pg_mar.get(f"{W}left")) if pg_mar is not None and pg_mar.get(f"{W}left") else 1080
    right = int(pg_mar.get(f"{W}right")) if pg_mar is not None and pg_mar.get(f"{W}right") else 1080
    return width, height, left, right


# --- checks -------------------------------------------------------------------

def check_blank_pages(root: ET.Element) -> CheckResult:
    issues: list[str] = []
    paragraphs = root.findall(f".//{W}p")
    body = root.find(f"{W}body")
    children = list(body) if body is not None else []

    # Consecutive empty paragraphs >= 5
    streak = 0
    for p in paragraphs:
        text = "".join(t.text or "" for t in p.findall(f".//{W}t"))
        if not text.strip():
            streak += 1
        else:
            streak = 0
    if streak >= 5:
        issues.append(f"{streak} consecutive empty paragraphs")

    # Trailing PageBreak at document end
    if children:
        last = children[-1]
        has_break = any(
            br.get(f"{W}type") == "page" for br in last.findall(f".//{W}br")
        )
        if has_break:
            issues.append("trailing PageBreak at document end")

    # Double break: a paragraph that carries BOTH a PageBreak and the section
    # break (pPr/sectPr) — page break + NEXT_PAGE = blank page.
    for ch in children:
        if ch.tag != f"{W}p":
            continue
        has_page_break = any(br.get(f"{W}type") == "page" for br in ch.findall(f".//{W}br"))
        carries_sect = ch.find(f"{W}pPr/{W}sectPr") is not None
        if has_page_break and carries_sect:
            issues.append("section-end paragraph has both PageBreak and sectPr (blank page)")

    return CheckResult(
        "blank-pages",
        not issues,
        "; ".join(issues) if issues else "no blank-page risks",
        "error",
    )


def check_cover_overflow(root: ET.Element) -> CheckResult:
    issues: list[str] = []
    first_section = root.find(f"{W}body/{W}sectPr")
    body = root.find(f"{W}body")
    if body is None:
        return CheckResult("cover-overflow", True, "no body")
    children = list(body)
    # Treat content before the first section break as the cover region.
    cover_end = next(
        (i for i, ch in enumerate(children) if ch.tag == f"{W}p" and ch.find(f"{W}pPr/{W}sectPr") is not None),
        len(children),
    )
    for p in children[:cover_end]:
        for r in p.findall(f".//{W}r"):
            sz = r.find(f"{W}rPr/{W}sz")
            if sz is not None and sz.get(f"{W}val") and int(sz.get(f"{W}val")) > 88:  # >44pt
                issues.append("cover font > 44pt (use calcTitleLayout)")
        spacing = p.find(f"{W}pPr/{W}spacing")
        if spacing is not None and spacing.get(f"{W}before"):
            before = int(spacing.get(f"{W}before"))
            if before > 5000:
                issues.append(f"cover spacing.before={before} twips (>5000, use calcCoverSpacing)")
    # trailing empty paragraphs in cover region
    trailing = 0
    for p in reversed(children[:cover_end]):
        text = "".join(t.text or "" for t in p.findall(f".//{W}t"))
        if not text.strip() and not p.findall(f".//{W}br"):
            trailing += 1
        else:
            break
    if trailing > 2:
        issues.append(f"{trailing} trailing empty paragraphs in cover")
    return CheckResult("cover-overflow", not issues, "; ".join(issues) if issues else "cover within budget")


def check_line_spacing(root: ET.Element) -> CheckResult:
    spacings: list[int] = []
    for p in root.findall(f".//{W}p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        if style is not None and style.get(f"{W}val", "").startswith("Heading"):
            continue
        spacing = p.find(f"{W}pPr/{W}spacing")
        if spacing is not None and spacing.get(f"{W}line"):
            spacings.append(int(spacing.get(f"{W}line")))
    if not spacings:
        return CheckResult("line-spacing", True, "no explicit line spacing")
    dominant = max(set(spacings), key=spacings.count)
    divergent = sum(1 for s in spacings if s != dominant)
    ratio = divergent / len(spacings)
    if ratio > 0.2:
        return CheckResult(
            "line-spacing",
            False,
            f"{divergent}/{len(spacings)} paragraphs diverge from dominant {dominant} twips",
            "warning",
        )
    return CheckResult("line-spacing", True, f"uniform at {dominant} twips")


def check_image_overflow(root: ET.Element) -> CheckResult:
    width, height, left, right = page_setup(root)
    usable = (width - left - right) * 1.05
    over = [cx for ext in root.findall(f".//{WP}extent") if (cx := int(ext.get("cx", "0"))) > usable]
    return CheckResult(
        "image-overflow", not over, f"{len(over)} image(s) wider than usable page" if over else "images within page"
    )


def check_image_aspect_ratio(docx_path: str, root: ET.Element) -> CheckResult:
    rels = read_rels(docx_path)
    issues: list[str] = []
    try:
        from PIL import Image as PILImage
        import io

        with zipfile.ZipFile(docx_path) as archive:
            for blip in root.findall(f".//{A}blip"):
                rid = blip.get(f"{R}embed")
                target = rels.get(rid or "", "").lstrip("/")
                if not target:
                    continue
                try:
                    with PILImage.open(io.BytesIO(archive.read(f"word/{target}"))) as img:
                        px_ratio = img.width / img.height
                except (KeyError, OSError, ValueError, ZeroDivisionError):
                    continue
                extent = blip.find(f".//{WP}extent")
                if extent is None:
                    continue
                cx, cy = int(extent.get("cx", "0")), int(extent.get("cy", "1"))
                if cx <= 0 or cy <= 0:
                    continue
                drift = abs(px_ratio - cx / cy) / max(px_ratio, 1e-9)
                if drift > 0.10:
                    issues.append(f"image {target} aspect drift {drift:.0%}")
    except ImportError:
        return CheckResult("image-aspect-ratio", True, "PIL unavailable — skipped", "info")
    return CheckResult(
        "image-aspect-ratio", not issues, "; ".join(issues) if issues else "aspect ratios preserved", "warning"
    )


def check_font_fallback(root: ET.Element) -> CheckResult:
    unsafe = {
        fonts.get(attr)
        for fonts in root.findall(f".//{W}rFonts")
        for attr in (f"{W}ascii", f"{W}hAnsi", f"{W}eastAsia", f"{W}cs")
        if (val := fonts.get(attr)) and val not in SAFE_FONTS
    }
    return CheckResult(
        "font-fallback",
        not unsafe,
        f"unsafe fonts: {', '.join(sorted(unsafe))}" if unsafe else "all fonts in safe list",
        "info",
    )


def check_heading_levels(root: ET.Element) -> CheckResult:
    levels: list[int] = []
    for p in root.findall(f".//{W}p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        if style is None:
            continue
        match = re.match(r"Heading\s*(\d)", style.get(f"{W}val", ""))
        if match:
            levels.append(int(match.group(1)))
    if len(levels) < 2:
        return CheckResult("heading-levels", True, "fewer than 2 headings — skipped", "info")
    jumps = [
        (levels[i], levels[i + 1])
        for i in range(len(levels) - 1)
        if levels[i + 1] - levels[i] > 1
    ]
    if jumps:
        return CheckResult("heading-levels", False, f"heading level jumps: {jumps}", "warning")
    return CheckResult("heading-levels", True, "heading hierarchy contiguous")


def check_shading_type(root: ET.Element) -> CheckResult:
    bad = [shd for shd in root.findall(f".//{W}shd") if shd.get(f"{W}val") == "solid"]
    return CheckResult(
        "shading-type", not bad, f"{len(bad)} w:shd val=solid (use clear)" if bad else "all shading CLEAR"
    )


def check_toc(root: ET.Element, docx_path: str) -> CheckResult:
    body_text = "".join(t.text or "" for t in root.findall(f".//{W}t"))
    has_toc_title = "目录" in body_text or "Table of Contents" in body_text
    has_toc_field = any(
        instr.text and "TOC" in instr.text.upper()
        for instr in root.findall(f".//{W}instrText")
    )
    heading_count = sum(
        1
        for p in root.findall(f".//{W}p")
        if (p.find(f"{W}pPr/{W}pStyle") is not None)
        and (p.find(f"{W}pPr/{W}pStyle").get(f"{W}val", "") or "").startswith("Heading")
    )
    if has_toc_title and not has_toc_field:
        return CheckResult("toc", False, "TOC title present but no TOC field", "error")
    if has_toc_field and heading_count == 0:
        return CheckResult("toc", False, "TOC field present but 0 Heading paragraphs", "error")
    if has_toc_field and not _headings_have_outline(docx_path):
        return CheckResult("toc", False, "Heading styles missing outlineLvl", "error")
    if has_toc_field and not read_settings_update_fields(docx_path):
        return CheckResult("toc", False, "settings.xml missing updateFields=true", "warning")
    return CheckResult("toc", True, "TOC structure ok" if has_toc_field else "no TOC expected")


def _headings_have_outline(docx_path: str) -> bool:
    """Heading styles must carry w:outlineLvl (H1=0, H2=1, H3=2) or Word's TOC
    update finds nothing. python-docx Heading styles set this; a doc where every
    heading style lacks it cannot produce a working TOC."""
    try:
        with zipfile.ZipFile(docx_path) as archive:
            styles = ET.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return True  # styles.xml unreadable — don't fabricate a failure
    heading_styles = [
        s
        for s in styles.findall(f"{W}style")
        if s.find(f"{W}name") is not None
        and (s.find(f"{W}name").get(f"{W}val") or "").startswith("heading")
    ]
    if not heading_styles:
        return True
    return any(s.find(f"{W}pPr/{W}outlineLvl") is not None for s in heading_styles)


def _check_table_margins(root: ET.Element) -> CheckResult:
    tables = root.findall(f".//{W}tbl")
    missing: list[int] = []
    for idx, table in enumerate(tables, start=1):
        has_margins = table.find(f"{W}tblPr/{W}tblCellMar") is not None
        cells = table.findall(f".//{W}tc")
        if not has_margins and cells:
            cell_ok = any(tc.find(f"{W}tcPr/{W}tcMar") is not None for tc in cells)
            if not cell_ok:
                missing.append(idx)
    return CheckResult(
        "table-margins",
        not missing,
        f"tables without cell margins: {missing}" if missing else "all tables have margins",
    )


def _check_table_cross_page(root: ET.Element) -> CheckResult:
    issues: list[str] = []
    # Skip the cover wrapper table (single exact-height cell, not a data table).
    for idx, table in enumerate(root.findall(f".//{W}tbl"), start=1):
        rows = table.findall(f"{W}tr")
        if len(rows) < 2 or all(row.find(f"{W}trPr/{W}trHeight") is not None for row in rows):
            continue
        header_ok = any(row.find(f"{W}trPr/{W}tblHeader") is not None for row in rows[:2])
        cant_split_ok = all(row.find(f"{W}trPr/{W}cantSplit") is not None for row in rows[1:])
        if not header_ok:
            issues.append(f"table {idx}: no tblHeader row")
        if not cant_split_ok:
            issues.append(f"table {idx}: rows missing cantSplit")
    return CheckResult(
        "table-cross-page",
        not issues,
        "; ".join(issues) if issues else "header repeat + cantSplit present",
    )


def _check_cjk_indent(root: ET.Element) -> CheckResult:
    cjk_body = 0
    missing = 0
    for p in root.findall(f".//{W}p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        if style is not None and (style.get(f"{W}val", "") or "").startswith("Heading"):
            continue
        text = "".join(t.text or "" for t in p.findall(f".//{W}t"))
        if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            continue
        # Skip short labels (captions, table cells, centered lines).
        if len(text) < 8 or p.find(f"{W}pPr/{W}jc") is not None:
            continue
        cjk_body += 1
        ind = p.find(f"{W}pPr/{W}ind")
        has_chars = ind is not None and ind.get(f"{W}firstLineChars") not in (None, "0")
        if not has_chars:
            missing += 1
    if cjk_body and missing / cjk_body > 0.2:
        return CheckResult(
            "cjk-indent",
            False,
            f"{missing}/{cjk_body} CJK paragraphs missing firstLineChars=200",
            "warning",
        )
    return CheckResult("cjk-indent", True, "CJK indent rules satisfied")


def _check_numbering_continuity(root: ET.Element) -> CheckResult:
    num_ids = [
        int(num.get(f"{W}val"))
        for num in root.findall(f".//{W}numPr/{W}numId")
        if num.get(f"{W}val") not in ("0", None)
    ]
    if not num_ids:
        return CheckResult("numbering-continuity", True, "no numbered lists")
    # Light check: no singleton list with a skipped id gap within the same style run.
    from collections import Counter

    counts = Counter(num_ids)
    gaps = {nid for nid in counts if nid not in (1, 0) and nid - 1 not in counts and nid - 1 >= 1}
    if gaps:
        return CheckResult("numbering-continuity", False, f"numId gaps: {sorted(gaps)}", "warning")
    return CheckResult("numbering-continuity", True, "numbering ids contiguous")


def _check_cleanliness(root: ET.Element) -> CheckResult:
    text = "".join(t.text or "" for t in root.findall(f".//{W}t"))
    matches = CLEANLINESS_RE.findall(text)
    return CheckResult(
        "cleanliness",
        not matches,
        f"placeholder tokens: {sorted(set(matches))}" if matches else "no placeholder tokens",
    )


def _check_content_quality(root: ET.Element) -> CheckResult:
    issues: list[str] = []
    paragraphs = ["".join(t.text or "" for t in p.findall(f".//{W}t")) for p in root.findall(f".//{W}p")]
    body_text = "\n".join(paragraphs)
    has_heading = any(p.startswith(("摘要", "引言", "结论", "Abstract", "Introduction")) for p in paragraphs)
    if len(paragraphs) > 8 and not has_heading:
        issues.append("document longer than a page but no 摘要/引言/结论 heading")
    for line in paragraphs:
        if line.strip() in ("略", "待补充", "暂无", "N/A", "TBD"):
            issues.append(f"placeholder line: {line.strip()}")
    return CheckResult(
        "content-quality", not issues, "; ".join(issues) if issues else "content structure ok", "warning"
    )


def run_all_checks(docx_path: str) -> list[CheckResult]:
    root = read_document_xml(docx_path)
    checks = [
        check_blank_pages(root),
        check_cover_overflow(root),
        check_line_spacing(root),
        check_image_overflow(root),
        check_font_fallback(root),
        check_heading_levels(root),
        check_shading_type(root),
        check_toc(root, docx_path),
        check_image_aspect_ratio(docx_path, root),
        _check_table_margins(root),
        _check_table_cross_page(root),
        _check_cjk_indent(root),
        _check_numbering_continuity(root),
        _check_cleanliness(root),
        _check_content_quality(root),
    ]
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="docx business-rule self-check")
    parser.add_argument("docx_path")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if not Path(args.docx_path).exists():
        print(json.dumps({"error": "File not found"}, ensure_ascii=False))
        sys.exit(1)

    try:
        results = run_all_checks(args.docx_path)
    except Exception as exc:  # noqa: BLE001 - report check failure instead of crashing
        print(json.dumps({"error": f"postcheck failed: {exc}"}, ensure_ascii=False))
        sys.exit(2)

    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"  {r}")
        errors = sum(1 for r in results if not r.passed and r.severity == "error")
        warnings = sum(1 for r in results if not r.passed and r.severity == "warning")
        print(f"  Passed {sum(1 for r in results if r.passed)}/{len(results)} | ❌ {errors} | ⚠️ {warnings}")

    has_errors = any(not r.passed and r.severity == "error" for r in results)
    has_warnings = any(not r.passed and r.severity == "warning" for r in results)
    if has_errors:
        sys.exit(2)
    if args.strict and has_warnings:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
