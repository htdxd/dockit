"""共享材料处理：hash staging、Parser 路由、隔离存储、任务内缓存与最小合并。

责任边界（实施计划 §6）：本模块只解析、合并、缓存任务内材料事实，决定
业务相关性/排版产物是 Material Planner 的职责。Skill 只获得只读
catalog/tool view，不拥有解析缓存。

本阶段实现（阶段 2/3/4/6）：
- 每个任务独立 workspace；源文件只读副本进 sources/<material_id>/original.<ext>
- 同 hash 材料只保留一份；同名/同 stem 文件互不覆盖
- Markdown/TXT 解析前复制安全相对引用资源，拒绝 `..` 越界与远程 URL
- 由 DocumentIR 单向生成现有 work/materials/<stem>.md 兼容投影
- md/txt 原生解析；pptx 复用 ppt-master parser；pdf 复用 pdf_docx_routing 的
  convert_pdf（MinerU 强依赖，失败即明确错误）；docx 富结构解析待阶段 4
  （当前以文本视图可用，旧 ingest/read 路径继续服务）

路径约定：manifest 与所有暴露路径必须是 workspace 相对路径；禁止写入
临时绝对路径。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from lxml import etree

from skill_toolbox.material_models import (
    SCHEMA_VERSION,
    Asset,
    Block,
    DocumentIR,
    SourceFormat,
)
from skill_toolbox.subprocess_utils import (
    child_env,
    process_group_kwargs,
    terminate_process_tree,
)

# 稳定错误码：这些码表示"选定 MinerU 路由后转换失败"，按计划 §3.1 必须
# 任务失败（fail-fast），不能登记成可继续的 warning。
MINERU_FATAL_CODES = {"MINERU_CLI_MISSING", "MINERU_TOKEN_MISSING", "MINERU_PARSE_FAILED"}

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

# 支持的源格式（md/txt/pptx/pdf 已接入共享解析；docx 走旧路径/阶段 4）。
SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".md",
    ".markdown",
    ".txt",
    ".pptx",
    *_REF_SAFE_SUFFIXES,
}

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
M_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


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


def _material_scope(material_id: str) -> str:
    """短目录/对象 scope 必须包含完整 material_id（含 source suffix）。"""
    return hashlib.sha256(material_id.encode("utf-8")).hexdigest()[:16]


def _block_id(material_id: str, order: int) -> str:
    return f"block-{_material_scope(material_id)}-{order}"


def _asset_id(material_id: str, locator: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{material_id}:{locator}:{content_hash}".encode()
    ).hexdigest()[:12]
    return f"asset-{_material_scope(material_id)}-{digest}"


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
    return "".join(
        node.text or ""
        for node in element.iter()
        if node.tag in {W_NS + "t", M_NS + "t"}
    ).strip()


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
    archive: zipfile.ZipFile, workspace: Path, material_id: str
) -> tuple[list[Asset], dict[str, Asset]]:
    assets_dir = material_id_dir(workspace, material_id) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets: list[Asset] = []
    by_member: dict[str, Asset] = {}
    for member in sorted(
        name for name in archive.namelist() if name.startswith("word/media/")
    ):
        out = assets_dir / PurePosixPath(member).name
        out.write_bytes(archive.read(member))
        content_hash = sha256_file(out)
        asset = Asset(
            id=_asset_id(material_id, member, content_hash),
            path=_relative_to(workspace, out),
            mime_type=_mime_for(out),
            sha256=content_hash,
            width=_image_width(out),
            height=_image_height(out),
            source_locator=f"docx:{member}",
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

    def __init__(
        self,
        workspace: Path,
        extra_env: dict[str, str] | None = None,
        mineru_token: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.extra_env = extra_env or {}
        # MinerU API Token（Sidecar 受限凭据）。只传给 PDF adapter 子进程，
        # 不进入 Agent 可见路径（实施计划 §3.1）。
        self._mineru_token = mineru_token
        # 任务内活动子进程（ppt parser / MinerU convert）。预处理经
        # asyncio.to_thread 执行时取消无法中断线程，Runtime 在 CancelledError
        # 时调 terminate_all() 杀掉直接子进程，避免残留（实施计划 §9.5）。
        self._active_procs: list[subprocess.Popen[bytes]] = []
        self._proc_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._irs: dict[str, DocumentIR] = {}
        self._manifest: dict[str, Any] = {"materials": [], "schema_version": SCHEMA_VERSION}
        self._compat: dict[str, str] = {}

    def _run_subprocess(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        """带注册/注销的同步 subprocess.run：供 to_thread 线程内调用，
        取消时可从 Runtime 侧 terminate_all() 杀掉直接子进程。"""
        if self._cancelled.is_set():
            raise RuntimeError("material parsing was cancelled")
        use_mineru_token = bool(kwargs.pop("use_mineru_token", False))
        env = child_env(
            {key: value for key, value in self.extra_env.items() if key != "MINERU_TOKEN"}
        )
        if use_mineru_token:
            token = self._mineru_token or self.extra_env.get("MINERU_TOKEN")
            if token:
                env["MINERU_TOKEN"] = token
        timeout = kwargs.pop("timeout", None)
        capture = kwargs.pop("capture_output", True)
        check = kwargs.pop("check", False)
        if capture:
            kwargs.setdefault("stdout", subprocess.PIPE)
            kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("env", env)
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        for key, value in process_group_kwargs().items():
            kwargs.setdefault(key, value)
        proc = subprocess.Popen(command, **kwargs)
        with self._proc_lock:
            self._active_procs.append(proc)
        if self._cancelled.is_set():
            terminate_process_tree(proc)
            raise RuntimeError("material parsing was cancelled")
        try:
            if timeout is None:
                out, err = proc.communicate()
            else:
                try:
                    out, err = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    terminate_process_tree(proc)
                    raise
            rc = proc.returncode or 0
            if check and rc != 0:
                raise subprocess.CalledProcessError(rc, proc.args, out, err)
            return subprocess.CompletedProcess(proc.args, rc, out, err)
        finally:
            with self._proc_lock:
                if proc in self._active_procs:
                    self._active_procs.remove(proc)

    def terminate_all(self) -> None:
        """取消/超时时终止所有活动子进程（MinerU、ppt parser 等）。"""
        self._cancelled.set()
        with self._proc_lock:
            active = list(self._active_procs)
        for proc in active:
            terminate_process_tree(proc)
        with self._proc_lock:
            self._active_procs.clear()

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
        materials_root = material_id_dir(self.workspace, ir.material_id)
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
                f"不支持的格式: {suffix or 'unknown'}（支持 pdf/docx/md/txt/pptx/常见图片）",
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

        source_dir = self.workspace / "sources" / _material_scope(material_id)
        source_dir.mkdir(parents=True, exist_ok=True)
        staged = source_dir / f"original{suffix}"
        shutil.copy2(source, staged)

        sha256 = sha256_file(staged)
        if suffix in {".md", ".markdown", ".txt"}:
            ir = self._parse_native_text(staged, source, suffix)
        elif suffix == ".pptx":
            ir = self._parse_native_pptx(staged, source, material_id, sha256)
        elif suffix == ".pdf":
            ir = self._parse_native_pdf(staged, source, material_id, sha256)
        elif suffix == ".docx":
            ir = self._parse_native_docx(staged, source, material_id, sha256)
        elif suffix in _REF_SAFE_SUFFIXES:
            ir = self._parse_native_image(staged, source, material_id, sha256)
        else:
            raise MaterialError(
                "MATERIAL_UNSUPPORTED",
                f"不支持的格式: {suffix or 'unknown'}（支持 pdf/docx/md/txt/pptx/常见图片）",
            )
        self._write_manifest()
        return ir

    def _parse_native_image(
        self, staged: Path, original: Path, material_id: str, sha256: str
    ) -> DocumentIR:
        asset = _materialize_asset_into(
            self.workspace, material_id, staged, f"original{staged.suffix.lower()}"
        ).model_copy(
            update={
                "source_locator": f"image:{original.name}",
                "surrounding_text": original.stem,
            }
        )
        return self._register(
            material_id,
            original,
            "image",
            sha256,
            [],
            [asset],
            [],
        )

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
            raise MaterialError(
                "MATERIAL_CORRUPT", f"ppt-master parser 缺失: {parser}"
            )
        out_root = material_id_dir(self.workspace, material_id)
        out_root.mkdir(parents=True, exist_ok=True)
        md_target = out_root / "parser.md"
        md_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = self._run_subprocess(
                [sys.executable, str(parser), str(staged), "-o", str(md_target)],
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaterialError(
                "MATERIAL_CORRUPT", f"ppt_to_md parser 调用失败: {exc}"
            )
        if completed.returncode or not md_target.is_file():
            raise MaterialError(
                "MATERIAL_CORRUPT",
                f"ppt_to_md parser 失败（rc={completed.returncode}）",
            )

        try:
            md_text = md_target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise MaterialError("MATERIAL_CORRUPT", f"parser.md 读取失败: {exc}") from None

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
                    id=_block_id(material_id, order),
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

    def _parse_native_pdf(
        self, staged: Path, original: Path, material_id: str, sha256: str
    ) -> DocumentIR:
        """PDF → 共享 DocumentIR：复用 pdf_docx_routing 的 convert_pdf 脚本
        （MinerU）。输出 docx 交给 _parse_native_docx 复用同一份结构解析。

        PDF 是 MinerU 强依赖（实施计划 §3.1）：MinerU CLI 不可用、Token 缺失
        或转换失败时抛 MaterialError（稳定错误码），由 Runtime 记为解析失败并
        fail-fast（任务直接失败），不降级、不登记成可继续的 warning。
        """
        parser = (
            Path(__file__).parent
            / "skill_defs"
            / "pdf_docx_routing"
            / "scripts"
            / "convert_pdf.py"
        )
        if not parser.is_file():
            raise MaterialError(
                "MINERU_PARSE_FAILED", "convert_pdf 脚本缺失，PDF 富解析不可用"
            )
        out_root = material_id_dir(self.workspace, material_id)
        out_root.mkdir(parents=True, exist_ok=True)
        md_dir = out_root / "native"
        md_dir.mkdir(parents=True, exist_ok=True)
        converted = md_dir / f"{safe_stem(original.stem)}.docx"
        try:
            completed = self._run_subprocess(
                [
                    sys.executable,
                    str(parser),
                    str(staged),
                    str(converted),
                    "auto",  # model 由脚本自身探测（同 convert_pdf 契约）
                    "true",
                    "true",
                    "true",
                    "ch",
                    "--internal-absolute-paths",
                ],
                timeout=1860,
                use_mineru_token=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise MaterialError(
                "MINERU_PARSE_FAILED", f"PDF 转换超时（{exc}）"
            ) from None
        except OSError as exc:
            raise MaterialError("MINERU_PARSE_FAILED", f"PDF 转换调用失败（{exc}）") from None
        if completed.returncode or not converted.is_file():
            detail = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            code = "MINERU_PARSE_FAILED"
            if any(k in detail.lower() for k in ("token", "auth", "401")):
                code = "MINERU_TOKEN_MISSING"
            raise MaterialError(code, detail or "MinerU 转换失败")
        # 复用 docx 结构解析（MinerU 产出的 docx 同样是 OOXML 包）；
        # source_format 保留 pdf（不写成 docx，IR 记录真实来源格式）。
        ir = self._parse_native_docx(converted, original, material_id, sha256, "pdf")
        entry = next(
            item
            for item in self._manifest["materials"]
            if item["material_id"] == material_id
        )
        entry["prepared_docx"] = self._rel(converted)
        return ir

    def _parse_native_docx(
        self,
        staged: Path,
        original: Path,
        material_id: str,
        sha256: str,
        format_for: SourceFormat | None = None,
    ) -> DocumentIR:
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
                archive, self.workspace, material_id
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
            locator_prefix = "pdf-mineru" if format_for == "pdf" else "docx"
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
                        block_type = "formula"
                        text = formula_text
                        warnings.append(f"{locator} 的 OMML 公式以线性文本投影")
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

        fmt = format_for or _format_for(original.suffix.lower())
        if fmt == "pdf":
            warnings.append("MinerU DOCX 未提供可靠页码/bbox；保留顺序与 OOXML 关系")
        return self._register(
            material_id,
            original,
            fmt,
            sha256,
            blocks,
            assets,
            warnings,
        )

    def _parse_native_text(
        self, staged: Path, original: Path, suffix: str
    ) -> DocumentIR:
        """md/txt 原生解析：标题/列表/表格/代码/图片引用 + 相对资源安全物化。"""
        try:
            md_text = staged.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            md_text = staged.read_text(encoding="utf-8", errors="replace")
        warnings: list[str] = []
        material_id = material_id_for(original)

        # 物化相对引用资源到 assets/（相对引用以原始文件目录为基准解析，
        # 拒绝逃逸源目录与远程 URL；staged 副本目录不含原始相对结构）
        assets: list[Asset] = []
        src_dir = original.parent.resolve()
        assets_dir = material_id_dir(self.workspace, material_id) / "assets"
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
                    path=self._rel(out),
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

        sha256 = sha256_file(staged)
        return self._register(
            material_id,
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
    return workspace / COMPAT_MATERIALS_DIR / _material_scope(material_id)


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
    asset_id = _asset_id(material_id, out.name, content_hash)
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
