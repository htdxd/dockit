from __future__ import annotations

import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V = "{urn:schemas-microsoft-com:vml}"
REQUIRED_PARTS = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}


def workspace_path(value: str) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(value)
    return path


def _package_target(target: str) -> str | None:
    value = target.replace("\\", "/")
    if value.startswith("/"):
        member = posixpath.normpath(value.lstrip("/"))
    else:
        member = posixpath.normpath(posixpath.join("word", value))
    if member == ".." or member.startswith("../"):
        return None
    return member


def _relationship_member(part: str) -> str:
    parent, name = posixpath.split(part)
    return posixpath.join(parent, "_rels", name + ".rels")


def _content_parts(members: set[str]) -> list[str]:
    return sorted(
        member
        for member in members
        if re.fullmatch(r"word/(?:document|header|footer)\d*\.xml", member)
    )


def _image_relationship_ids(root: ET.Element) -> set[str]:
    ids = {
        rid
        for node in root.iter(A + "blip")
        if (rid := node.get(R + "embed"))
    }
    ids.update(
        rid
        for node in root.iter(V + "imagedata")
        if (rid := node.get(R + "id"))
    )
    return ids


def _audit(source: Path) -> dict[str, object]:
    issues: list[str] = []
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        members = set(names)
        if duplicate_count := len(names) - len(members):
            issues.append(f"package contains {duplicate_count} duplicate member(s)")
        if corrupt := archive.testzip():
            issues.append(f"CRC failure: {corrupt}")
        for missing in sorted(REQUIRED_PARTS - members):
            issues.append(f"missing package part: {missing}")

        parsed: dict[str, ET.Element] = {}
        for member in sorted(REQUIRED_PARTS & members):
            try:
                parsed[member] = ET.fromstring(archive.read(member))
            except ET.ParseError as exc:
                issues.append(f"invalid XML {member}: {exc}")

        document = parsed.get("word/document.xml")
        content_roots: dict[str, ET.Element] = {}
        text_parts: list[str] = []
        for part in _content_parts(members):
            if part == "word/document.xml" and document is not None:
                root = document
            else:
                try:
                    root = ET.fromstring(archive.read(part))
                except ET.ParseError as exc:
                    issues.append(f"invalid XML {part}: {exc}")
                    continue
            content_roots[part] = root
            part_text = "".join(node.text or "" for node in root.iter(W + "t"))
            text_parts.append(part_text)
            if part != "word/document.xml":
                continue
            has_visible_object = any(
                document.find(".//" + tag) is not None
                for tag in (W + "tbl", W + "drawing", W + "pict")
            )
            if not part_text.strip() and not has_visible_object:
                issues.append("document has no visible content")

        for part, root in content_roots.items():
            referenced_ids = _image_relationship_ids(root)
            rels_member = _relationship_member(part)
            relationships: dict[str, ET.Element] = {}
            if rels_member in members:
                try:
                    rels_root = ET.fromstring(archive.read(rels_member))
                except ET.ParseError as exc:
                    issues.append(f"invalid XML {rels_member}: {exc}")
                else:
                    relationships = {
                        rel.get("Id", ""): rel for rel in rels_root if rel.get("Id")
                    }
            elif referenced_ids:
                issues.append(f"missing package part: {rels_member}")

            for rid in sorted(referenced_ids):
                if rid not in relationships:
                    issues.append(f"missing relationship: {part}::{rid}")
            for rid, rel in relationships.items():
                if not rel.get("Type", "").endswith("/image"):
                    continue
                if rel.get("TargetMode") == "External":
                    issues.append(f"external image relationship is not embedded: {part}::{rid}")
                    continue
                target = _package_target(rel.get("Target", ""))
                if target is None or target not in members:
                    issues.append(
                        f"broken image relationship {part}::{rid}: {rel.get('Target', '')}"
                    )

        document_rels = _relationship_member("word/document.xml")
        if document_rels in members:
            try:
                rels_root = ET.fromstring(archive.read(document_rels))
            except ET.ParseError:
                rels_root = None
            if rels_root is not None:
                for rel in rels_root:
                    rel_type = rel.get("Type", "")
                    if not rel_type.endswith(("/header", "/footer")):
                        continue
                    target = _package_target(rel.get("Target", ""))
                    if target is None or target not in members:
                        issues.append(
                            f"broken document relationship {rel.get('Id', '')}: "
                            f"{rel.get('Target', '')}"
                        )

        media_files = [name for name in names if name.startswith("word/media/")]

    text = "\n".join(text_parts)
    latex = len(re.findall(r"\$\$|\\(?:begin|frac|Psi|varPsi)", text))
    layout_tokens = len(re.findall(r"<\|(?:box|ref)_(?:start|end)\|>", text))
    if latex:
        issues.append(f"visible LaTeX markers remain: {latex}")
    if layout_tokens:
        issues.append(f"layout tokens remain: {layout_tokens}")
    return {
        "source": str(source.relative_to(Path.cwd())),
        "ok": not issues,
        "issues": issues,
        "latex_markers": latex,
        "layout_tokens": layout_tokens,
        "embedded_media": len(media_files),
        "visual_verification_required": True,
    }


def main() -> None:
    try:
        source = workspace_path(sys.argv[1])
        report = _audit(source)
    except (IndexError, FileNotFoundError, ValueError, zipfile.BadZipFile) as exc:
        report = {"ok": False, "issues": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False))
    if not report["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
