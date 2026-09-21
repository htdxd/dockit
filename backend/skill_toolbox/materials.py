"""任务材料的暂存、Parser 路由、IR 登记与缓存。

PDF/DOCX 保持 MinerU 主解析，原件机械视图独立提供；源文件只读。
格式读取在 parsers 中，资源路径在 material_assets 中，进程生命周期由 ProcessRunner 负责。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from skill_toolbox.material_assets import (
    _REF_SAFE_SUFFIXES,
    MaterialError,
    _asset_id,
    _format_for,
    _material_scope,
    _materialize_asset_into,
    _mime_for,
    material_id_dir,
    material_id_for,
    safe_stem,
)
from skill_toolbox.material_models import (
    SCHEMA_VERSION,
    Asset,
    Block,
    DocumentIR,
    SourceFormat,
)
from skill_toolbox.parsers import docx as docx_parser
from skill_toolbox.parsers import text as text_parser
from skill_toolbox.parsers.docx import (
    _check_native_gaps,
    _docx_assets,
    docx_native_text,
)
from skill_toolbox.parsers.text import project_content_md
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.tools.workspace import atomic_write_json, sha256_file

# 稳定错误码：这些码表示"选定 MinerU 路由后转换失败"，按计划 §3.1 必须
# 任务失败（fail-fast），不能登记成可继续的 warning。
MINERU_FATAL_CODES = {"MINERU_CLI_MISSING", "MINERU_TOKEN_MISSING", "MINERU_PARSE_FAILED"}

# 支持的上传格式；图片后缀与资源复制共用白名单。
SUPPORTED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".md",
    ".markdown",
    ".txt",
    *_REF_SAFE_SUFFIXES,
}


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

    def asset_path(self, asset_id: str) -> str | None:
        for ir in self.irs.values():
            asset = ir.asset_by_id(asset_id)
            if asset is not None:
                return asset.path
        return None


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
        cache_root: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.extra_env = extra_env or {}
        # MinerU API Token（Sidecar 受限凭据）。只传给 PDF adapter 子进程，
        # 不进入 Agent 可见路径（实施计划 §3.1）。
        self._mineru_token = mineru_token
        # MinerU 跨任务持久缓存根目录（实施计划 §6）；测试可注入 tmp 目录，
        # 默认 <app-data>/skill-toolbox-cache。
        from skill_toolbox.tools.mineru import default_cache_root

        self._cache_root = cache_root or default_cache_root()
        # 预处理经
        # asyncio.to_thread 执行时取消无法中断线程，Runtime 在 CancelledError
        # 时调 terminate_all() 杀掉直接子进程，避免残留（实施计划 §9.5）。
        self._runner = ProcessRunner()
        self._irs: dict[str, DocumentIR] = {}
        self._manifest: dict[str, Any] = {"materials": [], "schema_version": SCHEMA_VERSION}
        self._compat: dict[str, str] = {}

    def _run_subprocess(self, command: list[str], *, timeout=None, use_mineru_token=False):
        env = {key: value for key, value in self.extra_env.items() if key != 'MINERU_TOKEN'}
        if use_mineru_token:
            token = self._mineru_token or self.extra_env.get('MINERU_TOKEN')
            if token:
                env['MINERU_TOKEN'] = token
        return self._runner.run(command, timeout=timeout, extra_env=env)

    def terminate_active(self) -> None:
        self._runner.terminate_all()

    def terminate_all(self) -> None:
        self._runner.cancel()

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
        atomic_write_json(doc_path, ir.model_dump())
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
                f"不支持的格式: {suffix or 'unknown'}（支持 pdf/docx/md/txt/常见图片）",
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
        elif suffix in {".pdf", ".docx"}:
            ir = self._parse_mineru_document(staged, source, material_id, sha256)
        elif suffix in _REF_SAFE_SUFFIXES:
            ir = self._parse_native_image(staged, source, material_id, sha256)
        else:
            raise MaterialError(
                "MATERIAL_UNSUPPORTED",
                f"不支持的格式: {suffix or 'unknown'}（支持 pdf/docx/md/txt/常见图片）",
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

    def _parse_mineru_document(
        self, staged: Path, original: Path, material_id: str, sha256: str
    ) -> DocumentIR:
        """PDF/DOCX 的主解析共用 MinerU 与缓存，原件留作显式备用视图。"""
        from skill_toolbox.contracts.common import ToolError
        from skill_toolbox.tools.mineru import (
            MineruOptions,
            MineruService,
        )

        parser = Path(__file__).parent / "parsers" / "mineru_pdf.py"
        if not parser.is_file():
            raise MaterialError(
                "MINERU_PARSE_FAILED", "MinerU 转换脚本缺失，文档富解析不可用"
            )
        out_root = material_id_dir(self.workspace, material_id)
        out_root.mkdir(parents=True, exist_ok=True)
        md_dir = out_root / "native"
        md_dir.mkdir(parents=True, exist_ok=True)
        converted = md_dir / f"{safe_stem(original.stem)}.docx"

        service = MineruService(
            cache_root=self._cache_root,
            convert_script=parser,
            runner=lambda command, *, timeout: self._run_subprocess(
                command, timeout=timeout, use_mineru_token=True
            ),
        )
        try:
            result = service.convert(staged, converted, MineruOptions())
        except ToolError as exc:
            raise MaterialError(exc.code, str(exc)) from None
        except subprocess.TimeoutExpired as exc:
            raise MaterialError(
                "MINERU_PARSE_FAILED", f"文档转换超时（{exc}）"
            ) from None
        except OSError as exc:
            raise MaterialError("MINERU_PARSE_FAILED", f"文档转换调用失败（{exc}）") from None

        # 复用 docx 结构解析（MinerU 产出的 docx 同样是 OOXML 包）；
        # source_format 记录上传文件的真实格式，而非中间产物格式。
        source_format = _format_for(original.suffix.lower())
        ir = self._parse_native_docx(converted, original, material_id, sha256, source_format)
        if source_format == "pdf":
            self._add_pdf_original_images(staged, ir)
        else:
            native_blocks = docx_native_text(staged)
            _check_native_gaps(ir, "\n".join(b["text"] for b in native_blocks),
                               has_tables=any(b["type"] == "table" for b in native_blocks))
            with zipfile.ZipFile(staged) as package:
                originals, _ = _docx_assets(package, self.workspace, material_id, original=True)
            by_hash = {asset.sha256: asset for asset in ir.assets}
            for asset in originals:
                asset.caption = "DOCX 原件内嵌图片，未裁剪或缩放；作为照片使用前先看图确认。"
                if asset.sha256 in by_hash:
                    existing = by_hash[asset.sha256]
                    existing.source_locator = asset.source_locator
                    existing.caption = asset.caption
                else:
                    ir.assets.append(asset)
                    by_hash[asset.sha256] = asset
        self._write_ir(ir)
        entry = next(
            item
            for item in self._manifest["materials"]
            if item["material_id"] == material_id
        )
        entry["prepared_docx"] = self._rel(converted)
        entry["primary_parser"] = "mineru"
        entry["mineru_cache"] = result["status"]
        entry["mineru_cache_key"] = result["cache_key"]
        return ir

    def _add_pdf_original_images(self, source: Path, ir: DocumentIR) -> None:
        """保留 PDF 内嵌位图，避免选用重建 DOCX 中重采样过的照片。"""
        import pymupdf as fitz

        assets_dir = material_id_dir(self.workspace, ir.material_id) / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        seen_xrefs: set[int] = set()
        by_hash = {asset.sha256: asset for asset in ir.assets}
        try:
            with fitz.open(source) as pdf:
                _check_native_gaps(ir, "\n".join(page.get_text() for page in pdf))
                for page_index, page in enumerate(pdf):
                    for image_info in page.get_images():
                        xref = image_info[0]
                        if xref in seen_xrefs:
                            continue
                        seen_xrefs.add(xref)
                        image = pdf.extract_image(xref)
                        if not image:
                            continue
                        data = image["image"]
                        digest = hashlib.sha256(data).hexdigest()
                        locator = f"pdf:embedded-image:page[{page_index + 1}]:xref[{xref}]"
                        caption = (
                            f"PDF 第 {page_index + 1} 页原始内嵌图片"
                            f"（{image['width']}×{image['height']}，未裁剪或缩放）。"
                            "可能是照片、图标或整页扫描图；用于照片前请查看图片确认。"
                        )
                        if digest in by_hash:
                            asset = by_hash[digest]
                            if not asset.source_locator.startswith("pdf:embedded-image:"):
                                asset.source_locator = locator
                                asset.caption = caption
                                asset.page = page_index + 1
                            continue
                        out = assets_dir / f"pdf-original-{xref}.{image['ext']}"
                        out.write_bytes(data)
                        asset = Asset(
                            id=_asset_id(ir.material_id, locator, digest),
                            path=self._rel(out),
                            mime_type=_mime_for(out),
                            sha256=digest,
                            width=image["width"],
                            height=image["height"],
                            page=page_index + 1,
                            caption=caption,
                            source_locator=locator,
                        )
                        ir.assets.append(asset)
                        by_hash[digest] = asset
        except (fitz.FileDataError, OSError) as exc:
            ir.warnings.append(f"PDF 原始内嵌图片提取失败：{exc}")

    def _parse_native_docx(self, staged: Path, original: Path, material_id: str, sha256: str, format_for: SourceFormat | None = None) -> DocumentIR:
        parsed = docx_parser.read_document(staged, self.workspace, material_id, format_for)
        return self._register(material_id, original, format_for or _format_for(original.suffix.lower()), sha256, *parsed)

    def _parse_native_text(self, staged: Path, original: Path, suffix: str) -> DocumentIR:
        material_id = material_id_for(original)
        parsed = text_parser.read_document(staged, original, suffix, self.workspace, material_id)
        return self._register(material_id, original, _format_for(suffix), sha256_file(staged), *parsed)

    def _write_manifest(self) -> None:
        manifest_path = self.workspace / "work" / "materials" / "manifest.json"
        atomic_write_json(manifest_path, self._manifest)

    def catalog(self) -> MaterialCatalog:
        return MaterialCatalog(self.workspace, self._irs, self._manifest, self._compat)
