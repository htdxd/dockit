"""DOCX 结构、表格和嵌入资源提取；可用于原件及 MinerU 的中间产物。"""
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath

from lxml import etree
from skill_toolbox.material_assets import (
    MaterialError,
    _asset_id,
    _block_id,
    _mime_for,
    _relative_to,
    image_dimensions,
    material_id_dir,
)
from skill_toolbox.material_models import Asset, Block, DocumentIR, SourceFormat
from skill_toolbox.tools.workspace import sha256_file

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
M_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

def _docx_relationships(archive: zipfile.ZipFile, part_name: str) -> dict[str, str]:
    part = PurePosixPath(part_name)
    rels_name = str(part.parent / "_rels" / f"{part.name}.rels")
    try:
        root = etree.fromstring(archive.read(rels_name))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    relationships: dict[str, str] = {}
    for rel in root.iter(PKG_REL_NS + "Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        rel_id = rel.get("Id")
        target = rel.get("Target")
        if rel_id and target:
            relationships[rel_id] = posixpath.normpath(
                posixpath.join(str(part.parent), target)
            )
    return relationships


def _docx_content_items(root: etree._Element) -> list[etree._Element]:
    body = root.find(W_NS + "body")
    container = body if body is not None else root
    items: list[etree._Element] = []
    for child in container:
        if child.tag in {W_NS + "p", W_NS + "tbl"}:
            items.append(child)
        elif child.tag == W_NS + "sdt":
            items.extend(
                node
                for node in child.iter()
                if node.tag in {W_NS + "p", W_NS + "tbl"}
                and not any(
                    ancestor.tag in {W_NS + "p", W_NS + "tbl"}
                    for ancestor in node.iterancestors()
                    if ancestor is not child
                )
            )
    return items


def _docx_text(element: etree._Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag in {W_NS + "t", M_NS + "t"}:
            parts.append(node.text or "")
        elif node.tag == W_NS + "tab":
            parts.append("\t")
        elif node.tag in {W_NS + "br", W_NS + "cr"}:
            parts.append("\n")
        elif node.tag == W_NS + "p" and node is not element and parts:
            parts.append("\n")
    return "".join(parts).strip()


def _docx_heading_level(paragraph: etree._Element) -> int | None:
    props = paragraph.find(W_NS + "pPr")
    if props is None:
        return None
    outline = props.find(W_NS + "outlineLvl")
    if outline is not None and outline.get(W_NS + "val", "").isdigit():
        return min(int(outline.get(W_NS + "val")) + 1, 6)
    style = props.find(W_NS + "pStyle")
    style_id = style.get(W_NS + "val", "") if style is not None else ""
    match = re.search(r"(?:heading|标题)\s*([1-6])$", style_id, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _docx_is_list(paragraph: etree._Element) -> bool:
    props = paragraph.find(W_NS + "pPr")
    if props is None:
        return False
    if props.find(W_NS + "numPr") is not None:
        return True
    style = props.find(W_NS + "pStyle")
    style_id = style.get(W_NS + "val", "") if style is not None else ""
    return bool(re.search(r"(?:list|bullet|number|列表)", style_id, re.IGNORECASE))


def _docx_table_markdown(table: etree._Element) -> str:
    rows: list[list[str]] = []
    for row in table.findall("./" + W_NS + "tr"):
        cells = [
            _docx_text(cell).replace("|", "\\|").replace("\n", "<br>")
            for cell in row.findall("./" + W_NS + "tc")
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def _docx_assets(
    archive: zipfile.ZipFile, workspace: Path, material_id: str, *, original: bool = False
) -> tuple[list[Asset], dict[str, Asset]]:
    assets_dir = material_id_dir(workspace, material_id) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Asset] = []
    by_member: dict[str, Asset] = {}
    content_types = {}
    if "[Content_Types].xml" in archive.namelist():
        types = etree.fromstring(archive.read("[Content_Types].xml"))
        content_types = {node.get("PartName", "").lstrip("/"): node.get("ContentType")
                         for node in types if node.get("PartName")}
    for member in sorted(
        name for name in archive.namelist() if name.startswith("word/media/")
    ):
        out = assets_dir / (("docx-original-" if original else "") + PurePosixPath(member).name)
        out.write_bytes(archive.read(member))
        content_hash = sha256_file(out)
        asset = Asset(
            id=_asset_id(material_id, ("original:" if original else "") + member, content_hash),
            path=_relative_to(workspace, out),
            mime_type=content_types.get(member) or _mime_for(out),
            sha256=content_hash,
            **image_dimensions(out),
            source_locator=f"{'docx-original' if original else 'docx'}:{member}",
        )
        assets.append(asset)
        by_member[member] = asset
    return assets, by_member


def _docx_embedded_asset_ids(
    element: etree._Element,
    relationships: dict[str, str],
    assets: dict[str, Asset],
) -> list[str]:
    result: list[str] = []
    for node in element.iter():
        rel_id = node.get(R_NS + "embed")
        target = relationships.get(rel_id or "")
        asset = assets.get(target or "")
        if asset and asset.id not in result:
            result.append(asset.id)
    return result


def docx_native_text(source: Path) -> list[dict]:
    """原件机械文字视图；不调用 MinerU，也不覆盖主解析 IR。"""
    blocks = []
    with zipfile.ZipFile(source) as archive:
        parts = ["word/document.xml"] + sorted(name for name in archive.namelist()
            if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name))
        for part in parts:
            root = etree.fromstring(archive.read(part))
            for index, item in enumerate(_docx_content_items(root)):
                text = _docx_table_markdown(item) if item.tag == W_NS + "tbl" else _docx_text(item)
                if text:
                    blocks.append({"text": text, "type": "table" if item.tag == W_NS + "tbl" else "paragraph",
                                   "source_locator": f"docx-original:{part}#item[{index}]"})
    return blocks


def _check_native_gaps(ir: DocumentIR, native_text: str, has_tables: bool = False) -> None:
    """只提示可机械发现的差异，不把字符串命中当作完整性证明。"""
    primary = "\n".join(block.text for block in ir.blocks)
    numbers = lambda text: set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))
    if numbers(native_text) - numbers(primary) or (has_tables and not any(b.type == "table" for b in ir.blocks)):
        ir.warnings.append("NATIVE_TEXT_RECOMMENDED: 原件中的部分数字或表格结构未在主解析找到；"
                           "请先调用 read_material(view='native_text') 补查内容，再决定是否询问用户。")


def read_document(staged: Path, workspace: Path, material_id: str, format_for: SourceFormat | None = None):
    """Parse ordered OOXML blocks and link every drawing to an Asset."""
    warnings: list[str] = []
    blocks: list[Block] = []
    if not staged.is_file():
        raise MaterialError("MATERIAL_CORRUPT", "DOCX 文件不可读")
    try:
        archive = zipfile.ZipFile(staged)
    except zipfile.BadZipFile as exc:
        raise MaterialError("MATERIAL_CORRUPT", f"DOCX 解析失败: {exc}") from None
    with archive:
        assets, asset_by_member = _docx_assets(
            archive, workspace, material_id
        )
        parts = ["word/document.xml"]
        parts.extend(
            sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            )
        )
        order = 0
        locator_prefix = f"{format_for}-mineru" if format_for else "docx"
        for part_name in parts:
            try:
                root = etree.fromstring(archive.read(part_name))
            except (KeyError, etree.XMLSyntaxError) as exc:
                warnings.append(f"{part_name} 无法解析: {exc}")
                continue
            relationships = _docx_relationships(archive, part_name)
            for item_index, item in enumerate(_docx_content_items(root)):
                locator = f"{locator_prefix}:{part_name}#item[{item_index}]"
                asset_ids = _docx_embedded_asset_ids(
                    item, relationships, asset_by_member
                )
                if item.tag == W_NS + "tbl":
                    block_id = _block_id(material_id, order)
                    blocks.append(
                        Block(
                            id=block_id,
                            type="table",
                            order=order,
                            text=_docx_table_markdown(item),
                            asset_ids=asset_ids,
                            source_locator=locator,
                        )
                    )
                    order += 1
                    if item.find(".//" + W_NS + "gridSpan") is not None or item.find(
                        ".//" + W_NS + "vMerge"
                    ) is not None:
                        warnings.append(f"{locator} 含合并单元格，原生关系保留在 OOXML")
                    for asset_id in asset_ids:
                        blocks.append(
                            Block(
                                id=_block_id(material_id, order),
                                type="image",
                                order=order,
                                parent_id=block_id,
                                asset_ids=[asset_id],
                                source_locator=f"{locator}/image[{asset_id}]",
                            )
                        )
                        order += 1
                    continue

                text = _docx_text(item)
                formula_text = "".join(
                    node.text or "" for node in item.iter(M_NS + "t")
                ).strip()
                if formula_text:
                    warnings.append(f"{locator} 的 OMML 公式以线性文本投影")
                if formula_text and text == formula_text:
                    block_type = "formula"
                elif asset_ids:
                    block_type = "image"
                elif _docx_heading_level(item) is not None:
                    block_type = "heading"
                elif _docx_is_list(item):
                    block_type = "list"
                else:
                    block_type = "paragraph"
                if not text and not asset_ids:
                    continue
                level = _docx_heading_level(item) if block_type == "heading" else None
                blocks.append(
                    Block(
                        id=_block_id(material_id, order),
                        type=block_type,
                        order=order,
                        text=text,
                        level=level,
                        caption=text if block_type == "image" and text else None,
                        asset_ids=asset_ids,
                        source_locator=locator,
                    )
                )
                order += 1

    fmt = format_for or "docx"
    if fmt == "pdf":
        warnings.append("MinerU DOCX 未提供可靠页码/bbox；保留顺序与 OOXML 关系")
    return blocks, assets, warnings
