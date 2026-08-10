"""共享材料处理：hash staging、Parser 路由、隔离存储、任务内缓存与最小合并。

责任边界（实施计划 §6）：本模块只解析、合并、缓存任务内材料事实，决定
业务相关性/排版产物是 Material Planner 的职责。Skill 只获得只读
catalog/tool view，不拥有解析缓存。

本阶段实现（阶段 2）：
- 每个任务独立 workspace；源文件只读副本进 sources/<material_id>/original.<ext>
- 同 hash 材料只保留一份；同名/同 stem 文件互不覆盖
- Markdown/TXT 解析前复制安全相对引用资源，拒绝 `..` 越界与远程 URL
- 由 DocumentIR 单向生成现有 work/materials/<stem>.md 兼容投影
- PDF/DOCX/PPTX 的富解析在阶段 3/4/6 接入；本模块先提供统一路由骨架与
  原生 markdown 路径，MinerU/原生 docx 解析保持 tools.py 现有实现

路径约定：manifest 与所有暴露路径必须是 workspace 相对路径；禁止写入
临时绝对路径。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from skill_toolbox.material_models import (
    SCHEMA_VERSION,
    Asset,
    Block,
    DocumentIR,
    SourceFormat,
)

# 相对资源引用（Markdown 图片 / 相对链接）。inline 可带 title，
# 捕获到空白处即可；保留 ![]() 与 <img src="">
_MD_REF_RE = re.compile(
    r"!\[[^\]]*\]\(([^\s)\"]+)(?:\s+[\"'(].*?)?\)|<img[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

# 安全引用白名单：只物化这些后缀，其余相对链接按缺失引用处理（warning）。
_REF_SAFE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# 兼容投影目录：work/materials/<stem>.md（对齐旧 ingest 路径，阶段 3 前旧
# Skill 仍按此读取；该投影由新 IR 单向生成，不能反向解析覆盖 document.json）。
COMPAT_MATERIALS_DIR = "work/materials"

# 支持的源格式（阶段 2 已接：md/txt 原生；pdf/docx/pptx 在后续阶段接入）。
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".markdown", ".txt", ".pptx"}


class MaterialError(RuntimeError):
    """材料处理错误。code 为稳定错误码（实施计划 §9.1）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_stem(name: str) -> str:
    """安全化 stem：只把路径分隔符等不安全字符换成下划线，保留可读字符。

    Path(name).stem 在 Windows 上对 "a/b" 只取 "b"（/ 视作分隔符）；这里先
    把分隔符替换掉再取 stem，保证 "a/b" → "a_b"（不丢失目录语义）。
    """
    stem = re.sub(r'[/\\:*?"<>|\x00\s]', "_", name)
    stem = Path(stem).stem
    stem = stem.strip("._")
    return stem or "material"


def material_id_for(source: Path) -> str:
    """material_id = 源文件 SHA-256 全量（manifest 用）+ 安全化扩展名。

    目录名使用前 16 位；发生前缀冲突时扩展长度（阶段 7 验证，当前 16 位
    足够区分测试材料）。
    """
    return f"{sha256_file(source)}{source.suffix.lower()}"


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


class MaterialCatalog:
    """任务内材料事实的只读视图。

    由 MaterialService.prepare() 构建一次；Runtime 拥有生命周期，Skill 只读
    此 catalog / tool view，不拥有解析缓存（实施计划 §7）。
    """

    def __init__(
        self,
        workspace: Path,
        irs: dict[str, DocumentIR],
        manifest: dict[str, Any],
        compat_projection: dict[str, str],
    ) -> None:
        self.workspace = workspace
        self.irs = irs
        self.manifest = manifest
        self.compat_projection = compat_projection  # material_id -> 相对 md 路径

    def relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.workspace.resolve()))

    def ir_for(self, material_id: str) -> DocumentIR | None:
        return self.irs.get(material_id)


class MaterialService:
    """材料暂存、解析、合并与任务内缓存。

    生命周期由 Runtime 持有；同一任务内同 hash 材料只解析一次（manifest 记录
    多个原始名称）。解析完成后才进入 Agent loop。
    """

    def __init__(self, workspace: Path, extra_env: dict[str, str] | None = None) -> None:
        self.workspace = workspace.resolve()
        self.extra_env = extra_env or {}
        self._irs: dict[str, DocumentIR] = {}
        self._manifest: dict[str, Any] = {"materials": [], "schema_version": SCHEMA_VERSION}
        self._compat: dict[str, str] = {}

    def _rel(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.workspace))

    def _register(
        self,
        material_id: str,
        original: Path,
        source_format: SourceFormat,
        sha256: str,
        blocks: list[Block],
        assets: list[Asset],
        warnings: list[str],
        page_count: int | None = None,
    ) -> DocumentIR:
        """构造并缓存 DocumentIR，返回缓存中的实例（保证 prepare 返回与
        缓存引用一致，同 hash 复用走同一对象）。"""
        ir = DocumentIR(
            material_id=material_id,
            original_name=original.name,
            source_format=source_format,
            sha256=sha256,
            page_count=page_count,
            blocks=blocks,
            assets=assets,
            warnings=warnings,
        )
        self._irs[material_id] = ir
        self._write_ir(ir)
        # manifest 记录多个原始名称（同 hash 只解析一次）
        entry = next(
            (m for m in self._manifest["materials"] if m["material_id"] == material_id),
            None,
        )
        if entry is None:
            self._manifest["materials"].append(
                {
                    "material_id": material_id,
                    "original_names": [original.name],
                    "source_format": source_format,
                    "sha256": sha256,
                    "parsed": True,
                }
            )
        else:
            names = entry.setdefault("original_names", [])
            if original.name not in names:
                names.append(original.name)
        return ir

    def _write_ir(self, ir: DocumentIR) -> None:
        """原子写 document.json + 单向生成 content.md 兼容投影。

        目录名统一用材料 ID 前 16 位（manifest 记录完整 hash；前 16 位足够
        区分任务内材料，前缀冲突时扩展长度——实施计划 §7）。
        """
        materials_root = self.workspace / COMPAT_MATERIALS_DIR / ir.material_id[:16]
        materials_root.mkdir(parents=True, exist_ok=True)
        doc_path = materials_root / "document.json"
        tmp = doc_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(ir.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(doc_path)
        md_path = materials_root / "content.md"
        md_path.write_text(project_content_md(ir), encoding="utf-8")
        self._compat[ir.material_id] = self._rel(md_path)

    def prepare_material(self, source: Path) -> DocumentIR:
        """暂存 + 解析单个源文件，返回其 DocumentIR。

        同 hash 已解析则直接返回缓存（manifest 记录多个原始名称，不重复解析）。
        """
        if not source.is_file():
            raise MaterialError("MATERIAL_CORRUPT", f"材料不是有效文件: {source}")
        suffix = source.suffix.lower()
        # 先判断格式再暂存：格式不支持的文件存在时返回 MATERIAL_UNSUPPORTED
        if suffix not in SUPPORTED_SUFFIXES:
            raise MaterialError(
                "MATERIAL_UNSUPPORTED",
                f"不支持的格式: {suffix or 'unknown'}（支持 pdf/docx/md/txt/pptx）",
            )
        material_id = material_id_for(source)
        if material_id in self._irs:
            # 同 hash 不同文件名：登记原始名称，复用缓存（manifest 记录多个
            # 原始名称；不允许任务内同 hash 重复解析——实施计划 §9.2）
            for entry in self._manifest["materials"]:
                if entry["material_id"] == material_id:
                    names = entry.setdefault("original_names", [])
                    if source.name not in names:
                        names.append(source.name)
                    break
            return self._irs[material_id]

        source_dir = self.workspace / "sources" / material_id[:16]
        source_dir.mkdir(parents=True, exist_ok=True)
        staged = source_dir / f"original{suffix}"
        shutil.copy2(source, staged)

        sha256 = sha256_file(staged)
        if suffix in {".md", ".markdown", ".txt"}:
            ir = self._parse_native_text(staged, source, suffix)
        elif suffix == ".pptx":
            ir = self._parse_native_pptx(staged, source, material_id, sha256)
        else:
            # pdf/docx 富解析在阶段 3/4 接入；当前登记 warning 占位，
            # 由旧 ingest 路径（tools.py）继续服务，避免双路径同时解析。
            ir = self._register(
                material_id,
                source,
                _format_for(suffix),
                sha256,
                [],
                [],
                [f"{suffix} 富解析尚未接入共享层（阶段 3/4），继续走旧 ingest 路径"],
            )
        self._write_manifest()
        return ir

    def _parse_native_pptx(
        self, staged: Path, original: Path, material_id: str, sha256: str
    ) -> DocumentIR:
        """PPTX → 共享 DocumentIR：复用 ppt-master 成熟的 ppt_to_md parser
        （实施计划 §10.1：不再为 PPT 另跑一套 source_to_md 产生第二套事实）。

        parser 以 subprocess 调用（避免在 Runtime 进程内 import python-pptx
        与其私有 API）；输出 md + 关联媒体物化到本材料 assets/。
        """
        warnings: list[str] = []
        parser = Path(__file__).parent / "skill_defs" / "ppt-master" / "scripts" / "source_to_md" / "ppt_to_md.py"
        if not parser.is_file():
            return self._register(
                material_id,
                original,
                "pptx",
                sha256,
                [],
                [],
                [f"ppt-master parser 缺失: {parser}"],
            )
        out_root = self.workspace / "work" / "materials" / material_id[:16]
        out_root.mkdir(parents=True, exist_ok=True)
        md_target = out_root / "parser.md"
        md_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                [sys.executable, str(parser), str(staged), "-o", str(md_target)],
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._register(
                material_id,
                original,
                "pptx",
                sha256,
                [],
                [],
                [f"ppt_to_md parser 调用失败: {exc}"],
            )
        if completed.returncode or not md_target.is_file():
            warnings.append(f"ppt_to_md parser 失败（rc={completed.returncode}）")
            return self._register(material_id, original, "pptx", sha256, [], [], warnings)

        try:
            md_text = md_target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"parser.md 读取失败: {exc}")
            return self._register(material_id, original, "pptx", sha256, [], [], warnings)

        # 物化 parser 引用的媒体（<stem>_files/ 目录）到本材料 assets/
        assets: list[Asset] = []
        parser_media_root = md_target.parent / f"{md_target.stem}_files"
        assets_dir = out_root / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        asset_by_ref: dict[str, str] = {}
        if parser_media_root.is_dir():
            for media_file in sorted(parser_media_root.iterdir()):
                if media_file.suffix.lower() not in _REF_SAFE_SUFFIXES:
                    continue
                asset = _materialize_asset_into(self.workspace, material_id, media_file)
                assets.append(asset)
                asset_by_ref[media_file.name] = asset.id

        blocks: list[Block] = []
        for order, raw in enumerate(_parse_markdown_blocks(md_text)):
            block_type = raw["type"]
            asset_ids: list[str] = []
            if block_type == "image":
                for ref in raw.get("asset_refs", []):
                    ref_name = Path(ref).name
                    asset_id = asset_by_ref.get(ref_name)
                    if asset_id:
                        asset_ids.append(asset_id)
            blocks.append(
                Block(
                    id=f"b{order}",
                    type=block_type,
                    order=order,
                    text=raw.get("text", ""),
                    level=raw.get("level") if block_type == "heading" else None,
                    page=raw.get("page"),
                    asset_ids=asset_ids,
                    source_locator=f"pptx:{original.name}#b{order}",
                )
            )
        return self._register(material_id, original, "pptx", sha256, blocks, assets, warnings)

    def _parse_native_text(
        self, staged: Path, original: Path, suffix: str
    ) -> DocumentIR:
        """md/txt 原生解析：标题/列表/表格/代码/图片引用 + 相对资源安全物化。"""
        try:
            md_text = staged.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            md_text = staged.read_text(encoding="utf-8", errors="replace")
        warnings: list[str] = []

        # 物化相对引用资源到 assets/（相对引用以原始文件目录为基准解析，
        # 拒绝逃逸源目录与远程 URL；staged 副本目录不含原始相对结构）
        assets: list[Asset] = []
        src_dir = original.parent.resolve()
        assets_dir = self.workspace / COMPAT_MATERIALS_DIR / material_id_for(original)[:16] / "assets"
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
            out = assets_dir / src.name
            try:
                shutil.copy2(src, out)
            except OSError as exc:
                warnings.append(f"引用 {rel} 物化失败: {exc}")
                continue
            asset_id = f"asset-{len(assets)}"
            asset_by_ref[rel] = asset_id
            assets.append(
                Asset(
                    id=asset_id,
                    path=self._rel(out),
                    mime_type=_mime_for(src),
                    sha256=sha256_file(out),
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
                    id=f"b{order}",
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

        sha256 = sha256_file(staged)
        return self._register(
            material_id_for(original),
            original,
            _format_for(suffix),
            sha256,
            blocks,
            assets,
            warnings,
        )

    def _write_manifest(self) -> None:
        manifest_path = self.workspace / "work" / "materials" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(manifest_path)

    def catalog(self) -> MaterialCatalog:
        return MaterialCatalog(self.workspace, self._irs, self._manifest, self._compat)


def _format_for(suffix: str) -> SourceFormat:
    if suffix == ".md" or suffix == ".markdown":
        return "md"
    if suffix == ".txt":
        return "txt"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pptx":
        return "pptx"
    return "image"


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }
    return mime.get(ext, "application/octet-stream")


def material_id_dir(workspace: Path, material_id: str) -> Path:
    """材料 IR 根目录（work/materials/<id16>/）；统一命名，避免各处拼接。"""
    return workspace / COMPAT_MATERIALS_DIR / material_id[:16]


def _materialize_asset_into(
    workspace: Path, material_id: str, src: Path, out_name: str | None = None
) -> Asset:
    """把媒体物化到材料 assets/ 并登记 Asset（内容 hash 派生 id）。

    asset_id 由材料 ID + 来源 locator + 内容 hash 派生，不能只用原始文件名
    （实施计划 §7）。返回登记后的 Asset。
    """
    material_root = material_id_dir(workspace, material_id)
    assets_dir = material_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    out = assets_dir / (out_name or src.name)
    if not out.exists():
        shutil.copy2(src, out)
    content_hash = sha256_file(out)
    asset_id = f"asset-{material_id[:8]}-{content_hash[:12]}"
    return Asset(
        id=asset_id,
        path=_relative_to(workspace, out),
        mime_type=_mime_for(out),
        sha256=content_hash,
        width=_image_width(out),
        height=_image_height(out),
    )


def _relative_to(workspace: Path, path: Path) -> str:
    return str(path.resolve().relative_to(workspace.resolve()))


def _image_width(path: Path) -> int | None:
    from skill_toolbox.tools import _image_size

    dims = _image_size(path)
    return dims[0] if dims else None


def _image_height(path: Path) -> int | None:
    from skill_toolbox.tools import _image_size

    dims = _image_size(path)
    return dims[1] if dims else None


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
