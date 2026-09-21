"""Markdown/TXT 解析与只读投影；相对图片始终限制在原材料目录内。"""
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from skill_toolbox.material_assets import (
    _REF_SAFE_SUFFIXES,
    MaterialError,
    _asset_id,
    _block_id,
    _mime_for,
    _relative_to,
    material_id_dir,
)
from skill_toolbox.material_models import Asset, Block, DocumentIR
from skill_toolbox.tools.workspace import sha256_file

_MD_REF_RE = re.compile(
    r"!\[[^\]]*\]\(([^\s)\"]+)(?:\s+[\"'(].*?)?\)|<img[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

def _ref_targets(md_text: str) -> list[str]:
    """提取 md 中的本地引用路径；忽略远程/data: URI。"""
    targets: list[str] = []
    for m in _MD_REF_RE.finditer(md_text):
        raw = (m.group(1) or m.group(2) or "").split("#")[0].split("?")[0]
        if not raw or raw.startswith(("http://", "https://", "data:")):
            continue
        targets.append(raw)
    return targets


def _parse_markdown_blocks(
    md_text: str, page: int | None = None, markdown_image_pattern: str = "!["
) -> list[dict[str, Any]]:
    """把 Markdown 顺序投影为最小 Block 列表（heading/paragraph/list/table/code/image）。

    无原生结构（PDF/扫描）时的保守表示：按顺序生成块，表格以 Markdown
    投影文本保留，图片引用登记为 image 块（asset 物化由调用方完成）。
    实现保持线性扫描，不引入 O(n²) 配对（实施计划 §9.3）。
    """
    blocks: list[dict[str, Any]] = []
    order = 0
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        refs = _ref_targets(line)
        if refs:
            blocks.append(
                {
                    "type": "image",
                    "text": line,
                    "order": order,
                    "asset_refs": refs,
                    "page": page,
                }
            )
            order += 1
            i += 1
            continue
        if stripped.startswith("```"):
            # fenced code block
            fence = stripped[3:].strip().split()[0] if len(stripped) > 3 else ""
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过闭合围栏
            blocks.append(
                {
                    "type": "code",
                    "text": "\n".join(code_lines),
                    "order": order,
                    "level": fence or None,
                    "page": page,
                }
            )
            order += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            blocks.append(
                {
                    "type": "heading",
                    "text": heading.group(2),
                    "order": order,
                    "level": len(heading.group(1)),
                    "page": page,
                }
            )
            order += 1
            i += 1
            continue
        if stripped.startswith("|") and "|" in stripped:
            # 管道表格：收集连续表行
            table_lines = [lines[i]]
            i += 1
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                table_lines.append(lines[i])
                i += 1
            blocks.append(
                {
                    "type": "table",
                    "text": "\n".join(table_lines),
                    "order": order,
                    "page": page,
                }
            )
            order += 1
            continue
        if re.match(r"^[-*+]\s+", stripped) or re.match(r"^\d+[.)]\s+", stripped):
            # 列表：收集连续列表行（保持简单，不做嵌套）
            list_lines = [lines[i]]
            i += 1
            while i < len(lines) and (
                re.match(r"^\s*[-*+]\s+", lines[i])
                or re.match(r"^\s*\d+[.)]\s+", lines[i])
            ):
                list_lines.append(lines[i])
                i += 1
            blocks.append(
                {
                    "type": "list",
                    "text": "\n".join(list_lines),
                    "order": order,
                    "page": page,
                }
            )
            order += 1
            continue
        # 普通段落（合并连续非空行）
        para_lines = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() and not _ref_targets(lines[i]):
            para_lines.append(lines[i])
            i += 1
        blocks.append(
            {
                "type": "paragraph",
                "text": " ".join(p.strip() for p in para_lines),
                "order": order,
                "page": page,
            }
        )
        order += 1
    return blocks


def project_content_md(ir: DocumentIR) -> str:
    """把 DocumentIR 单向投影为 Agent 顺序阅读的 Markdown（兼容投影）。

    不是事实真源：document.json 是唯一事实源，content.md 不可反向解析覆盖。
    首行标题使用原始文件名；无 heading 块（如纯 TXT）时正文仍可读。
    """
    first_heading = next((b for b in ir.blocks if b.type == "heading"), None)
    lines: list[str] = [f"# {first_heading.text}" if first_heading else f"# {ir.original_name}"]
    lines.append("")
    for block in ir.blocks:
        if block.type == "heading":
            lines.append(f"{'#' * min(block.level or 2, 6)} {block.text}")
        elif block.type == "page_break":
            lines.append("\n---\n")
        elif block.type == "image":
            for asset_id in block.asset_ids:
                asset = ir.asset_by_id(asset_id)
                if asset:
                    lines.append(f"![{block.caption or block.text}]({asset.path})")
        elif block.type == "code":
            # level 承载语言；投影时作为围栏标注
            lines.append(f"```{block.level or ''}")
            lines.append(block.text)
            lines.append("```")
        elif block.type == "list":
            for item in block.text.splitlines():
                stripped = item.strip()
                if not stripped.startswith(("-", "*", "+")):
                    lines.append(f"- {stripped}")
                else:
                    lines.append(stripped)
        elif block.type == "table":
            lines.append(block.text)
        else:
            lines.append(block.text)
        lines.append("")
    return "\n".join(lines)


def read_document(staged: Path, original: Path, suffix: str, workspace: Path, material_id: str):
    """md/txt 原生解析：标题/列表/表格/代码/图片引用 + 相对资源安全物化。"""
    try:
        raw = staged.read_bytes()
        md_text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeDecodeError:
        try:
            md_text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise MaterialError("MATERIAL_CORRUPT", "文本编码无法识别，请另存为 UTF-8 后上传。") from exc
    warnings: list[str] = []

    # 物化相对引用资源到 assets/（相对引用以原始文件目录为基准解析，
    # 拒绝逃逸源目录与远程 URL；staged 副本目录不含原始相对结构）
    assets: list[Asset] = []
    src_dir = original.parent.resolve()
    assets_dir = material_id_dir(workspace, material_id) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_by_ref: dict[str, str] = {}
    for rel in _ref_targets(md_text):
        if rel in asset_by_ref:
            continue
        src = (src_dir / rel).resolve()
        try:
            src.relative_to(src_dir)
        except ValueError:
            warnings.append(f"引用 {rel} 逃出源目录，已忽略")
            continue
        if not src.is_file():
            warnings.append(f"引用 {rel} 不存在（缺失资源）")
            continue
        if src.suffix.lower() not in _REF_SAFE_SUFFIXES:
            warnings.append(f"引用 {rel} 不是支持的媒体格式，已忽略")
            continue
        rel_scope = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:10]
        out = assets_dir / f"{rel_scope}-{src.name}"
        try:
            shutil.copy2(src, out)
        except OSError as exc:
            warnings.append(f"引用 {rel} 物化失败: {exc}")
            continue
        content_hash = sha256_file(out)
        asset_id = _asset_id(material_id, rel, content_hash)
        asset_by_ref[rel] = asset_id
        assets.append(
            Asset(
                id=asset_id,
                path=_relative_to(workspace, out),
                mime_type=_mime_for(src),
                sha256=content_hash,
            )
        )

    blocks: list[Block] = []
    for order, raw in enumerate(_parse_markdown_blocks(md_text)):
        block_type = raw["type"]
        asset_ids: list[str] = []
        if block_type == "image":
            for ref in raw.get("asset_refs", []):
                asset_id = asset_by_ref.get(ref)
                if asset_id:
                    asset_ids.append(asset_id)
        blocks.append(
            Block(
                id=_block_id(material_id, order),
                type=block_type,
                order=order,
                text=raw.get("text", ""),
                # code 块的 level 承载语言（python/json…）；heading 的 level
                # 是标题层级 1-6。非 heading 一律不写 level，避免字符串污染
                # 整数字段（Block.level 类型为 int | None）。
                level=raw.get("level") if block_type == "heading" else None,
                page=raw.get("page"),
                asset_ids=asset_ids,
                source_locator=f"{suffix.lstrip('.')}:{original.name}#b{order}",
            )
        )

    return blocks, assets, warnings
