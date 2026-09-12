from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from skill_toolbox.models import ImageContent, ToolCall, ToolResult
from skill_toolbox.policy import PolicyViolation, WorkspacePolicy
from skill_toolbox.subprocess_utils import (
    child_env,
    process_group_kwargs,
    terminate_process_tree,
)

MAX_TEXT_BYTES = 1_000_000
MAX_IMAGE_BYTES = 12_000_000
# .docx 是 zip 容器，read 只解 document.xml 与媒体清单，不整包载入内存，
# 所以允许带照片的简历（几 MB）被读取；文本类文件仍按 1MB 限制。
MAX_DOCX_BYTES = 64 * 1024 * 1024
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".py",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".svg",
    ".xml",
    ".rs",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
}
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _image_size(path: Path) -> tuple[int, int] | None:
    """读取图片像素尺寸（纯标准库：JPEG SOF 段 / PNG IHDR），失败返回 None。"""
    import struct  # PNG/JPEG 两分支都要用，不能只在 PNG 分支内 import（JPEG 会 UnboundLocalError）

    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                # marker 前缀的 0xFF 可重复出现（填充字节），跳过；marker 落在 i 处
                while i < len(data) and data[i] == 0xFF:
                    i += 1
                if i + 1 >= len(data):
                    break
                marker = data[i]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    # SOF 段：marker(1) + 段长(2) + precision(1) + height(2) + width(2)
                    h, w = struct.unpack(">HH", data[i + 4 : i + 8])
                    return (w, h) if w and h else None
                # 其它带长度段：跳过 2 字节段长 + 段长本身（i+1 指向段长首字节）
                seg_len = struct.unpack(">H", data[i + 1 : i + 3])[0]
                i += 1 + seg_len
        return None
    except (struct.error, IndexError):
        return None


def _relative_to_cwd(path: Path, root: Path | None = None) -> str:
    """返回相对 workspace 根的路径（子进程 cwd 即 workspace），无法相对化时用绝对路径。

    root 显式传入（如 policy.root）时以它为准，避免依赖 cwd 与根目录恰好一致。
    """
    base = (root or Path.cwd()).resolve()
    try:
        return str(path.resolve().relative_to(base))
    except ValueError:
        return str(path)


def _extract_docx_media(docx_path: Path, dest_dir: Path, root: Path) -> list[dict[str, Any]]:
    """从 .docx 提取内嵌媒体清单并把图片物化到 dest_dir，返回元数据列表。

    简历旧文档常内嵌人像照，但照片字节封在 zip 里，模型既看不到也无法引用。
    这里按 document.xml 中 r:embed 首次出现顺序排序媒体（人像通常靠前），
    物化到工作区 work/_media/<docx名>/ 下，模型可 read 或作为 fields.photo 路径。
    portrait_likely 是给无视觉模型的启发式：近方形/竖版且非极小（简历人像
    通常 1:1 裁剪），帮助它不用看图也能挑出人像。root 为 workspace 根，
    用于把产物路径相对化。
    """
    try:
        with zipfile.ZipFile(docx_path) as z:
            all_names = z.namelist()
            names = set(all_names)
            document_xml = z.read("word/document.xml") if "word/document.xml" in names else b""
            rels_xml = z.read("word/_rels/document.xml.rels") if "word/_rels/document.xml.rels" in names else b""
            # 保持 namelist 归档顺序（zip 内媒体通常按写入顺序排），别用 set 迭代，
            # 否则同名 tie-break 时排序不稳定。
            media_members = [
                n for n in all_names
                if n.startswith("word/media/") and not n.endswith("/")
            ]
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Not a readable .docx: {exc}") from None

    # rId → 媒体目标（zip 内完整路径），来自 document.xml.rels。Target 是相对
    # word/ 目录的（如 media/photo.png），归一化成 word/media/photo.png 才能
    # 与 namelist 的 member 直接比对。
    target_by_rid: dict[str, str] = {}
    if rels_xml:
        try:
            rel_root = etree.fromstring(rels_xml)
            for rel in rel_root:
                rid = rel.get("Id")
                target = (rel.get("Target") or "").lstrip("/")
                if rid and target.startswith("media/"):
                    target_by_rid[rid] = f"word/{target}" if not target.startswith("word/") else target
        except etree.XMLSyntaxError:
            pass

    # document.xml 中 a:blip 的 r:embed 属性首次出现顺序（r:embed 是带命名空间的属性）
    embed_order: dict[str, int] = {}
    if document_xml:
        try:
            doc_root = etree.fromstring(document_xml)
            idx = 0
            for el in doc_root.iter():
                rid = el.get(R_NS + "embed")
                if rid:
                    embed_order.setdefault(rid, idx)
                    idx += 1
        except etree.XMLSyntaxError:
            pass

    dest_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for member in media_members:
        try:
            with zipfile.ZipFile(docx_path) as z:
                raw = z.read(member)
        except (KeyError, zipfile.BadZipFile):
            continue
        if not raw:
            continue
        out_path = dest_dir / Path(member).name
        try:
            out_path.write_bytes(raw)
        except OSError:
            continue
        width = height = None
        dims = _image_size(out_path)
        if dims:
            width, height = dims
        rid = next((r for r, tgt in target_by_rid.items() if tgt == member), None)
        aspect = (width / height) if width and height else None
        portrait_likely = bool(
            aspect is not None
            and 0.75 <= aspect <= 1.35
            and (width or 0) >= 80
            and (height or 0) >= 80
        )
        # a:blip 的 r:embed 是属性不是文本节点；这里从根遍历补查 embed 属性，
        # 与上面的 embed_order 解析保持一致（iter 属性名即带命名空间的展开名）。
        entries.append({
            "name": Path(member).name,
            # 工作区相对路径（模型可直接 read / 写进 fields.photo）；无法相对化
            # 时回退绝对路径。
            "path": _relative_to_cwd(out_path, root),
            "size": len(raw),
            "width": width,
            "height": height,
            "aspect": round(aspect, 2) if aspect else None,
            "portrait_likely": portrait_likely,
            "order": embed_order.get(rid, 10**6),
        })

    entries.sort(key=lambda e: (e["order"], -e["size"]))
    for entry in entries:
        entry.pop("order", None)
    return entries


def _extract_docx_text(path: Path) -> str:
    """从 .docx 提取纯文本（w:t 拼接，段落换行分隔）。

    模型经常需要读取用户上传的旧简历/参考 docx 来抽取信息，而 docx 是
    zip 容器不能按文本直读。这里用标准库解包 + lxml 提 w:t，给 read 工具
    一个轻量的 docx 文本视图（不含图片/表格样式，够做信息抽取）。
    """
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Not a readable .docx: {exc}") from None
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f".docx document.xml parse failed: {exc}") from None
    paragraphs: list[str] = []
    for p in root.iter(W_NS + "p"):
        text = "".join(t.text or "" for t in p.iter(W_NS + "t"))
        paragraphs.append(text)
    return "\n".join(paragraphs)


# ---------------- 材料萃取（ingest） ----------------
#
# 这是保留给旧 DOCX/Markdown 调用者的兼容投影；任务上传材料的事实源是
# Runtime 预处理生成的 DocumentIR，不在此处再次解析 PDF 或运行 MinerU。
#
# 约定（对齐 resume_pro 已验证的 media 元数据模式）：
#   manifest["media"][i] = {name, path(工作区相对), width, height, aspect,
#                           portrait_likely, order}
# 无视觉模型按 width/height/aspect/portrait_likely 自动选图，不询问用户。

INGEST_DIR = "work/materials"
INGEST_MANIFEST = "manifest.json"  # 相对 materials/ 目录（ingest 入口的路径基准不一致，避免拼接错位）

# 图片引用在 md 文本中的两种形态：![alt](path) 与 <img src="path">。
# inline 引用可带 title（![alt](path "title")），捕获到空白处即可。
_MD_IMG_RE = re.compile(
    r"!\[[^\]]*\]\(([^\s)\"]+)(?:\s+[\"'(].*?)?\)|<img[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _md_link_targets(md_text: str) -> list[str]:
    """提取 md 文本中引用的本地资源路径（图片/相对链接），仅保留工作区内可解析的。"""
    targets: list[str] = []
    for m in _MD_IMG_RE.finditer(md_text):
        raw = m.group(1) or m.group(2)
        raw = raw.split("#")[0].split("?")[0]
        if not raw or raw.startswith(("http://", "https://", "data:")):
            continue
        targets.append(raw)
    return targets


def _ingest_docx(source: Path, materials: Path) -> tuple[str, list[Path]]:
    """docx → md 文本 + 媒体物化到 materials/_media/。返回 (md_text, 媒体路径列表)。

    用 lxml 走 w:tbl 表格 → HTML 表格、w:p 文本、r:embed 媒体（复用 docx 的
    提取逻辑），不引第三方依赖（mammoth/pandoc 都不装）。
    """
    import html as _html

    lines: list[str] = []
    media_paths: list[Path] = []
    try:
        with zipfile.ZipFile(source) as z:
            xml = z.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Not a readable .docx: {exc}") from None
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f".docx document.xml parse failed: {exc}") from None

    def _has_ancestor(el, tag: str) -> bool:
        node = el.getparent()
        while node is not None:
            if node.tag == tag:
                return True
            node = node.getparent()
        return False

    media_dir = materials / "_media"
    for p in root.iter():
        if p.tag == W_NS + "tbl":
            # 只输出最外层表格；嵌套表格（w:tbl 在 w:tc 里）不再单独输出，
            # 否则内层行会漏进外层并多出一个孤立 <table>。
            if _has_ancestor(p, W_NS + "tbl"):
                continue
            rows = []
            for tr in p.iter(W_NS + "tr"):
                # 只取本表格直接辖下的行；嵌套表格（w:tbl 在 w:tc 里）的行由
                # 内层自己的 w:tbl 处理（但内层 tbl 因 _has_ancestor 被跳过）。
                # 这里按直接子关系收集，避免内层行/单元格漏进外层表。
                if tr.getparent() is not p:
                    continue
                cells = [
                    _html.escape("".join(t.text or "" for t in tc.iter(W_NS + "t")))
                    for tc in tr.iter(W_NS + "tc")
                    if tc.getparent() is tr
                ]
                if cells:
                    rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            if rows:
                lines.append("<table>" + "".join(rows) + "</table>")
            continue
        if p.tag == W_NS + "p":
            # 跳过表格内的段落（其文本已由 <table> 承载），避免单元格文字重复
            if _has_ancestor(p, W_NS + "tbl"):
                continue
            text = "".join(t.text or "" for t in p.iter(W_NS + "t")).strip()
            if text:
                lines.append(text)
    # 媒体物化（含人像启发式元数据，后续写进 manifest）
    try:
        media = _extract_docx_media(source, media_dir, materials.parent)
        media_paths = [materials.parent / m["path"] for m in media]
    except (ValueError, OSError):
        media = []
    return "\n\n".join(lines), media_paths


def _ingest_markdown(source: Path, materials: Path) -> tuple[str, list[Path]]:
    """md → 文本 + 物化本地图片引用到 materials/_media/。返回 (md_text, 媒体路径列表)。

    md 里的图片是相对路径（如 images/fig1.png），模型需要的是可引用的磁盘
    路径——物化（复制）到 _media/ 统一命名，避免源目录结构漂移。
    """
    try:
        md_text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        md_text = source.read_text(encoding="utf-8", errors="replace")
    media_paths: list[Path] = []
    media_dir = materials / "_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    for rel in _md_link_targets(md_text):
        src = source.parent / rel
        src = src.resolve()
        try:
            src.relative_to(source.parent.resolve())
        except ValueError:
            continue  # 逃出源目录的引用忽略
        if not src.is_file():
            continue
        out = media_dir / src.name
        try:
            shutil.copy2(src, out)
        except OSError:
            continue
        media_paths.append(out)
    return md_text, media_paths


def resolve_mineru_cli() -> list[str]:
    """解析 mineru-open-api 可执行入口，返回 argv 前缀（不含子命令）。

    Windows 上 npm 全局装的 mineru-open-api 是 .cmd 包装器，CreateProcess
    不能直接跑 .cmd → 优先 node + 包内 JS 入口，退化为 cmd.exe /c。
    Sidecar 的 MinerU preflight 使用这一解析，保证启动门判定一致。
    """
    cli = shutil.which("mineru-open-api")
    if not cli:
        raise RuntimeError(
            "未找到 mineru-open-api。请安装：npm install -g mineru-open-api，"
            "或在设置页「第三方服务」确认 MinerU Token 已填写。"
        )
    cli_path = Path(cli)
    if cli_path.suffix.lower() in {".cmd", ".bat"}:
        js_bin = cli_path.parent / "node_modules" / "mineru-open-api" / "bin" / "mineru-open-api"
        node = shutil.which("node")
        if node and js_bin.is_file():
            return [node, str(js_bin)]
        return ["cmd.exe", "/c", str(cli_path)]
    return [str(cli_path)]


def _materialize_refs_from_md(md_path: Path, dest_dir: Path) -> list[Path]:
    """把 md 引用的本地图片物化到 dest_dir，返回物化后的路径列表（与源 stem 无耦合）。"""
    try:
        md_text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    paths: list[Path] = []
    for rel in _md_link_targets(md_text):
        src = (md_path.parent / rel).resolve()
        try:
            src.relative_to(md_path.parent.resolve())
        except ValueError:
            continue
        if not src.is_file() or src.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            continue
        out = dest_dir / src.name
        if out.name in seen:
            continue
        seen.add(out.name)
        try:
            if not out.exists():
                shutil.copy2(src, out)
            paths.append(out)
        except OSError:
            continue
    return paths


def _media_meta(path: Path, root: Path, order: int) -> dict[str, Any]:
    """单张图片的元数据（对齐 resume_pro 的 media 清单字段）。"""
    dims = _image_size(path)
    width, height = dims or (None, None)
    aspect = (width / height) if width and height else None
    portrait_likely = bool(
        aspect is not None and 0.75 <= aspect <= 1.35
        and (width or 0) >= 80 and (height or 0) >= 80
    )
    return {
        "name": path.name,
        "path": _relative_to_cwd(path, root),
        "size": path.stat().st_size,
        "width": width,
        "height": height,
        "aspect": round(aspect, 2) if aspect else None,
        "portrait_likely": portrait_likely,
        "order": order,
    }


def _write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    """原子写 manifest（temp + replace）：并发 read .pdf 时避免半写损坏。"""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)


def ingest_material(source: Path, materials: Path) -> dict[str, Any]:
    """萃取单个源文件到 work/materials/，返回描述（供横幅与 read 复用）。

    幂等：已萃取（manifest 里存在）直接复用，重跑零成本。
    返回 {source, format, ok, md_path, media: [meta...], cached, error}
    """
    materials.mkdir(parents=True, exist_ok=True)
    manifest_path = materials / INGEST_MANIFEST  # work/materials/manifest.json
    manifest: dict[str, Any] = {"sources": []}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {"sources": []}
    key = source.resolve()
    existing = next((s for s in manifest["sources"] if Path(s.get("source", "")).resolve() == key), None)
    # md_path 相对 materials/ 目录（<ws>/work/materials/<stem>.md）
    if existing and existing.get("md_path") and Path(materials / existing["md_path"]).is_file():
        existing["cached"] = True
        return existing

    suffix = source.suffix.lower()
    try:
        if suffix == ".docx":
            md_text, media_paths = _ingest_docx(source, materials)
        elif suffix in {".md", ".markdown", ".txt"}:
            md_text, media_paths = _ingest_markdown(source, materials)
        else:
            raise ValueError(f"ingest 不支持的格式: {suffix or 'unknown'}（支持 docx/md/txt）")
    except Exception as exc:
        entry = {"source": str(source), "format": suffix.lstrip("."), "ok": False, "error": str(exc)}
        manifest["sources"].append(entry)
        _write_manifest(manifest_path, manifest)
        raise

    # 写 md 文件（统一命名 <stem>.md）
    md_path = materials / f"{source.stem}.md"
    md_path.write_text(md_text, encoding="utf-8")
    media: list[dict[str, Any]] = []
    for order, mp in enumerate(media_paths):
        media.append(_media_meta(mp, materials.parent, order))
    media.sort(key=lambda m: (m["order"], -m["size"]))
    for m in media:
        m.pop("order", None)
    entry = {
        "source": str(source),
        "format": suffix.lstrip("."),
        "ok": True,
        "md_path": f"{source.stem}.md",  # 相对 materials/ 目录（幂等校验按此拼接）
        "media": media,
    }
    manifest["sources"] = [s for s in manifest["sources"] if Path(s.get("source", "")).resolve() != key]
    manifest["sources"].append(entry)
    _write_manifest(manifest_path, manifest)
    return entry


def _manifest_entries(materials: Path) -> list[dict[str, Any]]:
    manifest_path = materials / INGEST_MANIFEST
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return manifest.get("sources", [])


def _materialize_md_media(md_path: Path, root: Path) -> list[dict[str, Any]]:
    """物化 md 里引用的本地图片到 work/_media/<md名>/，返回媒体清单（与 docx 同模式）。"""
    dest = root / "work" / "_media" / md_path.stem
    paths = _materialize_refs_from_md(md_path, dest)
    media: list[dict[str, Any]] = []
    for order, out in enumerate(paths):
        media.append(_media_meta(out, root, order))
    return media


class ToolRegistry:
    def __init__(
        self,
        policy: WorkspacePolicy,
        skill_dir: Path = Path("."),
        scripts: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        env: dict[str, str] | None = None,
        script_timeouts: dict[str, float] | None = None,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self.policy = policy
        self.skill_dir = skill_dir
        self.scripts = scripts or {}
        # Non-secret extra env vars merged into exec_cmd subprocesses.
        self.extra_env = env or {}
        # 每个 action 的显式超时（manifest script spec 的 timeout_seconds）。
        # 未声明的 action 沿用 communicate 默认 300s（外层 runtime 仍按默认
        # 短超时提前终止）。MinerU/Office 长任务靠声明值覆盖。
        self.script_timeouts = script_timeouts or {}
        # 模型能力（来自运行时解析的 capabilities）。vision=false 时硬性拒绝
        # 图片 read，保证 Provider 不收到 image payload（实施计划 §15.3 回归
        # 断言），而不是只靠 Prompt 自律。未提供时默认放行（历史测试兼容）。
        self.vision = (capabilities or {}).get("vision", True)

    def action_timeout(self, action: str) -> float:
        """声明过的 action 超时；未声明返回 0（由 runtime 走默认短超时）。"""
        return self.script_timeouts.get(action, 0.0)

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "read":
                return await asyncio.to_thread(self._read, call)
            if call.name == "ingest":
                return await asyncio.to_thread(self._ingest, call)
            if call.name == "write":
                return await asyncio.to_thread(self._write, call)
            if call.name == "edit":
                return await asyncio.to_thread(self._edit, call)
            if call.name == "spec_append":
                return await asyncio.to_thread(self._spec_append, call)
            if call.name == "exec_cmd":
                return await self._exec_cmd_async(call)
            return self._result(call, False, f"Unknown data tool: {call.name}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._result(call, False, str(exc))

    async def _exec_cmd_async(self, call: ToolCall) -> ToolResult:
        """Run declared scripts with cooperative cancellation so runtime
        timeouts terminate the spawned process instead of leaking it."""
        proc_holder: dict[str, subprocess.Popen[bytes] | None] = {"proc": None}
        cancelled = threading.Event()

        def runner() -> ToolResult:
            if cancelled.is_set():
                return self._result(call, False, "Script cancelled before start")
            proc = subprocess.Popen(
                self._build_cmd(call),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.policy.root),
                env=child_env(self.extra_env),
                close_fds=True,
                **process_group_kwargs(),
            )
            proc_holder["proc"] = proc
            if cancelled.is_set():
                self._kill(proc)
                return self._result(call, False, "Script cancelled during start")
            # 脚本内部可能自设超时（MinerU 1800s 等）；communicate 用声明值，
            # 避免此处提前杀掉合法长任务。未声明的沿用默认 300s（此时外层
            # runtime 短超时才是真正生效的界限）。
            timeout = self.action_timeout(str(call.arguments.get("action", ""))) or 300
            try:
                stdout_b, stderr_b = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                return self._result(call, False, f"Script timed out: {call.arguments.get('action')}")
            rc = proc.returncode or 0
            return self._result(
                call,
                rc == 0,
                json.dumps(
                    {
                        "exit_code": rc,
                        "stdout": stdout_b.decode("utf-8", errors="replace")[-8000:],
                        "stderr": stderr_b.decode("utf-8", errors="replace")[-2000:],
                    },
                    ensure_ascii=False,
                ),
            )

        task = asyncio.get_running_loop().run_in_executor(None, runner)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Runtime timed out (asyncio.wait_for) — kill the subprocess so it
            # doesn't keep consuming resources while we report the timeout.
            cancelled.set()
            self._kill(proc_holder["proc"])
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, OSError):
                pass
            raise

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        terminate_process_tree(proc)

    def _build_cmd(self, call: ToolCall) -> list[str]:
        action = call.arguments.get("action")
        if not action:
            # 模型常把 action 误放进 args 内层（{"args": {"action": ...}}），
            # 若直接 KeyError 会让模型无法自我纠正，宽容读取并给出明确指引。
            action = dict(call.arguments.get("args", {})).get("action")
        if not action:
            raise ValueError(
                "exec_cmd 缺少 action：action 必须放在顶层 arguments"
                '（如 {"action": "fill_resume", "args": {...}}），'
                "不要写进 args 内层。"
            )
        action = str(action)
        args = dict(call.arguments.get("args", {}))
        if action not in self.scripts:
            raise ValueError(f"Action is not allowed: {action}")
        entry_rel, argv_template = self.scripts[action]
        entry = self.skill_dir / "scripts" / entry_rel
        if not entry.is_file():
            entry = self.skill_dir / "custom_scripts" / entry_rel
        if not entry.is_file():
            raise ValueError(f"Script not found for action: {action}")
        rendered = [self._render(token, args) for token in argv_template]
        # 模型漏传参数时，_render 会把 {placeholder} 原样留在命令行里（例如
        # MinerU 收到字面量 --pages {pages} 而报 invalid page number）。
        # 在这里直接拒绝，给模型一个明确"缺哪个参数"的错误，而不是把占位符
        # 传给脚本当合法值用。
        missing = [
            token for token, value in zip(argv_template, rendered) if "{" in value
        ]
        if missing:
            raise ValueError(
                f"Action '{action}' 缺少必要参数，未渲染的占位符: {missing}。"
                f"argv 模板为 {list(argv_template)}，请把占位符对应的 key 传进 args。"
            )
        cmd = [sys.executable, str(entry)]
        cmd.extend(rendered)
        if env_args := args.get("env", {}):
            if not isinstance(env_args, dict):
                raise ValueError("env must be an object mapping variable names to values")
            cmd.extend(f"{name}={value}" for name, value in env_args.items())
        return cmd

    def _result(
        self,
        call: ToolCall,
        success: bool,
        content: str,
        images: list[ImageContent] | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            success=success,
            content=content,
            images=images or [],
        )

    def _read(self, call: ToolCall) -> ToolResult:
        path_str = str(call.arguments["path"])
        try:
            path = self.policy.require_file(path_str)
        except PolicyViolation:
            path = self.policy.find_readonly(path_str)
            if path is None:
                raise
        suffix = path.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            if not self.vision:
                # 无视觉模型硬性拒绝图片 read：不能把 base64 图片发给无视觉
                # Provider（实施计划 §3.3/§15.3）。材料图片仍可由 Agent 读其
                # 元数据（IR/caption/文件名）并据此做保守决策。
                raise ValueError(
                    "当前模型不支持图像理解（vision=false）：不能读取图片。"
                    "请基于图片的 caption、文件名或材料 IR 元数据做保守选择，"
                    "或换用支持 Vision 的模型。"
                )
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 12 MB read limit")
            media_type = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            image = ImageContent(
                media_type=media_type,
                base64_data=base64.b64encode(path.read_bytes()).decode("ascii"),
            )
            return self._result(call, True, f"Read image: {path.name}", [image])
        if suffix == ".docx":
            if path.stat().st_size > MAX_DOCX_BYTES:
                raise ValueError("docx exceeds the 64 MB read limit")
            content = _extract_docx_text(path)
            # 物化内嵌媒体到 work/_media/<docx名>/，返回元数据（含人像启发式）。
            # 仅当 docx 在工作区内（材料/产物）才物化；只读 skill 目录跳过。
            media: list[dict[str, Any]] = []
            if path.is_relative_to(self.policy.root):
                try:
                    media = _extract_docx_media(
                        path, self.policy.root / "work" / "_media" / path.stem, self.policy.root
                    )
                except (ValueError, OSError):
                    media = []
            return self._result(
                call,
                True,
                json.dumps(
                    {
                        "content": content,
                        "format": "docx-text",
                        "lines": content.count("\n") + 1,
                        "media": media,
                    },
                    ensure_ascii=False,
                ),
            )
        if suffix == ".pdf":
            raise ValueError(
                "PDF 已在 Agent loop 前由共享材料层解析；不要再次 read/ingest PDF。"
                "请读取 [MATERIALS] 横幅列出的 content.md 或 document.json。"
            )
        if suffix == ".md":
            if path.stat().st_size > MAX_TEXT_BYTES:
                raise ValueError("Text file exceeds the 1 MB read limit")
            md_text = path.read_text(encoding="utf-8", errors="replace")
            lines = md_text.splitlines()
            offset = int(call.arguments.get("offset", 0))
            limit = int(call.arguments.get("limit", 500))
            content = "\n".join(lines[offset : offset + limit])
            # md 内嵌图片物化到 work/_media/<md名>/，返回清单（与 docx 同模式）。
            # 阶段 3 起 md 已由共享层解析（work/materials/<id>/assets/），这里
            # 只物化 read 直接读到的 md（如兼容投影之外的原始 md），不重复
            # 生成第二套事实。
            media: list[dict[str, Any]] = []
            if path.is_relative_to(self.policy.root):
                media = _materialize_md_media(path, self.policy.root)
            return self._result(
                call,
                True,
                json.dumps(
                    {
                        "content": content,
                        "format": "markdown",
                        "lines": len(lines),
                        "media": media,
                    },
                    ensure_ascii=False,
                ),
            )
        if suffix not in TEXT_SUFFIXES:
            raise ValueError(f"Unsupported read format: {suffix or 'unknown'}")
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("Text file exceeds the 1 MB read limit")
        lines = path.read_text(encoding="utf-8").splitlines()
        offset = int(call.arguments.get("offset", 0))
        limit = int(call.arguments.get("limit", 500))
        content = "\n".join(lines[offset : offset + limit])
        return self._result(
            call,
            True,
            json.dumps(
                {"content": content, "offset": offset, "total_lines": len(lines)},
                ensure_ascii=False,
            ),
        )

    def _ingest(self, call: ToolCall) -> ToolResult:
        """ingest：把上传材料萃取成统一 IR（md + 媒体 + manifest）。"""
        path_str = str(call.arguments["path"])
        try:
            path = self.policy.require_file(path_str)
        except PolicyViolation:
            raise ValueError(f"ingest 仅支持工作区内文件: {path_str}")
        if path.suffix.lower() == ".pdf":
            return self._result(
                call,
                False,
                "PDF 已由共享材料层解析；请读取 [MATERIALS] 中的 IR 路径，禁止重复 MinerU。",
            )
        try:
            entry = ingest_material(path, self.policy.root / INGEST_DIR)
        except Exception as exc:  # noqa: BLE001 - 失败信息要完整回给模型
            return self._result(call, False, f"ingest 失败: {exc}")
        if not entry.get("ok"):
            return self._result(call, False, f"ingest 失败: {entry.get('error')}")
        return self._result(
            call,
            True,
            json.dumps(entry, ensure_ascii=False),
        )

    def _write(self, call: ToolCall) -> ToolResult:
        args = self._coerce_write_args(call.arguments)
        path = self.policy.resolve(str(args["path"]))
        content = str(args["content"])
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("write only supports text files")
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Content exceeds the 1 MB write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
        return self._result(call, True, f"Wrote {path.relative_to(self.policy.root)}")

    # spec_append：docx 规格增量构建。模型每次只提交一小块 blocks（1-8 个），
    # 后端合并进 work/spec.json 并校验——模型不再手拼整个巨型 spec JSON，
    # 避免单次工具参数过大在生成/传输中被截断（这是 docx 超轮的头号根因）。
    # 允许的 block 类型与 build_docx 的 blocks[] 对齐（见 spec-schema.md）。
    _SPEC_BLOCK_TYPES = {
        "heading": ("text", "level"),
        "paragraph": ("text",),
        "image": ("source", "caption", "width_mm"),
        "table": ("caption", "headers", "rows", "widths_pct"),
        "formula": ("latex", "caption"),
    }
    # 每个 block 类型必填字段（其余可选；caption 一律可选，表头/行/公式必填）
    _SPEC_REQUIRED = {
        "heading": ("text",),
        "paragraph": ("text",),
        "image": ("source",),
        "table": ("headers", "rows"),
        "formula": ("latex",),
    }
    _SPEC_META_KEYS = (
        "title", "subtitle", "author", "date", "language",
        "complexity", "scene", "cover", "sections",
    )
    _SPEC_MAX_BLOCKS_PER_CALL = 8

    def _spec_append(self, call: ToolCall) -> ToolResult:
        args = dict(call.arguments)
        spec_rel = str(args.get("spec") or "work/spec.json")
        path = self.policy.resolve(spec_rel)
        if path.suffix.lower() != ".json":
            raise ValueError("spec 必须是 .json 文件（如 work/spec.json）")

        spec: dict[str, Any] = {}
        if path.is_file():
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"work/spec.json 已是损坏 JSON（{exc.msg}）：请用 write 重写骨架，"
                    "或用 edit 修复后再 spec_append。"
                ) from None
            if not isinstance(spec, dict):
                raise ValueError("work/spec.json 顶层必须是 JSON 对象")

        # 顶层 meta 合并（首次调用可带 title/complexity/cover/sections…）
        for key in self._SPEC_META_KEYS:
            if key in args and args[key] is not None:
                spec[key] = args[key]

        # blocks 追加 + 逐项校验（出错即拒绝，不写半成品）
        blocks = args.get("blocks")
        if blocks is not None:
            if not isinstance(blocks, list):
                raise ValueError("blocks 必须是数组；每次传 1-8 个 block")
            if len(blocks) > self._SPEC_MAX_BLOCKS_PER_CALL:
                raise ValueError(
                    f"blocks 一次最多 {self._SPEC_MAX_BLOCKS_PER_CALL} 个，"
                    f"本次 {len(blocks)} 个——请分批 spec_append。"
                )
            for i, block in enumerate(blocks):
                if not isinstance(block, dict) or "type" not in block:
                    raise ValueError(
                        f"blocks[{i}] 缺少 'type'，必须是 "
                        f"{sorted(self._SPEC_BLOCK_TYPES)} 之一"
                    )
                btype = block["type"]
                allowed = self._SPEC_BLOCK_TYPES.get(btype)
                if allowed is None:
                    raise ValueError(
                        f"blocks[{i}].type='{btype}' 不支持，允许 "
                        f"{sorted(self._SPEC_BLOCK_TYPES)}"
                    )
                required_fields = self._SPEC_REQUIRED.get(btype, ())
                for field in required_fields:
                    if field not in block or block[field] in (None, ""):
                        raise ValueError(f"blocks[{i}]（type={btype}）缺必需字段 '{field}'")
                unknown = set(block) - {btype} - set(allowed) - {"type"}
                if unknown:
                    raise ValueError(
                        f"blocks[{i}]（type={btype}）有未知字段 {sorted(unknown)}，"
                        f"允许 {allowed}"
                    )
                # image source 存在性预检：模型自造/抄漏 hash 前缀的路径（如
                # 丢了 download_assets 返回的 8 位前缀）会拖到 build_docx 才报
                # FileNotFoundError，然后进入 9 步修路径循环。在这里当场拒绝
                # 并提示相近的真实文件名，模型一次就能纠正。
                if btype == "image":
                    src = str(block.get("source", ""))
                    if src and not src.startswith(("http://", "https://", "data:")):
                        img_path = (
                            Path(src) if Path(src).is_absolute()
                            else self.policy.root / src
                        )
                        if not img_path.is_file():
                            assets_dir = self.policy.root / "work" / "assets"
                            cands: list[str] = []
                            if assets_dir.is_dir():
                                stem = Path(src).name
                                cands = [
                                    f.name for f in sorted(assets_dir.iterdir())
                                    if f.name.endswith(stem[-16:])
                                ][:3]
                            raise ValueError(
                                f"blocks[{i}] image source 不存在: {src}。"
                                "图片路径必须用 download_assets 返回的 path"
                                "（形如 work/assets/<8位hex>-<文件名>），"
                                "不得自造或改写 hash。"
                                + (
                                    f" work/assets 下相近文件: {cands}"
                                    if cands else
                                    " 先重新调 download_assets 拿真实 path"
                                )
                            )
            spec.setdefault("blocks", []).extend(blocks)

        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        total = len(spec.get("blocks", []))
        return self._result(
            call,
            True,
            json.dumps(
                {"ok": True, "spec": spec_rel, "total_blocks": total},
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _coerce_write_args(arguments: dict) -> dict:
        """write 容错：模型偶尔把 {path, content} 包进 _invalid_json 等内部
        key（值可能是 dict 或 JSON 字符串），顶层缺 path/content 时尽量恢复，
        恢复不了给清晰错误而不是裸 KeyError（后者模型无法自我纠正）。
        """
        args = dict(arguments)
        if "path" in args and "content" in args:
            return args
        for key, value in args.items():
            if key in ("path", "content"):
                continue
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(value, dict) and "path" in value:
                args.setdefault("path", value["path"])
                args.setdefault("content", value.get("content", ""))
                return args
        missing = [k for k in ("path", "content") if k not in args]
        # 单次 write 塞整个巨型 JSON（图片多/全文长）时，工具参数在生成/传输中
        # 易被截断导致解析失败、path 丢失。给明确的分步建议而不是让模型盲目重试。
        payload = json.dumps(args, ensure_ascii=False) if args else ""
        hint = ""
        if len(payload) > 4000 or any(
            isinstance(v, str) and len(v) > 2000 for v in args.values()
        ):
            hint = (
                " 若内容很大（图片多/正文长），请拆成多次 write/edit：先 write 骨架，"
                "再用 edit 逐段追加，不要一次 write 塞整个巨型 JSON。"
            )
        raise ValueError(
            f"write 缺少参数 {missing}：path/content 必须放在顶层 arguments，"
            "不要包进 _invalid_json 等其它字段。示例："
            '{"path": "work/plans/content-plan.json", "content": "..."}' + hint
        )

    def _edit(self, call: ToolCall) -> ToolResult:
        path = self.policy.require_file(str(call.arguments["path"]))
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("edit only supports text files")
        content = path.read_text(encoding="utf-8")
        old_text = str(call.arguments["old_text"])
        new_text = str(call.arguments["new_text"])
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        if count > 1 and not bool(call.arguments.get("replace_all", False)):
            raise ValueError("old_text is not unique; set replace_all=true")
        updated = content.replace(
            old_text, new_text, -1 if call.arguments.get("replace_all") else 1
        )
        # JSON 文件在 edit 后校验：模型常用 edit 改 resume_data.json，改坏会让
        # 下游 fill 报 JSON 错再自愈，浪费整轮。这里直接拒绝写入并提示用 write。
        if path.suffix.lower() == ".json":
            try:
                json.loads(updated)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"edit 会破坏 JSON 结构（第 {exc.lineno} 行：{exc.msg}），未写入。"
                    "请改用 write 整文件重写，或修正 old_text/new_text 后再 edit。"
                ) from None
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(updated, encoding="utf-8")
        temp.replace(path)
        return self._result(call, True, f"Edited {path.relative_to(self.policy.root)}")

    def _render(self, token: str, args: dict[str, Any]) -> str:
        for key, value in args.items():
            token = token.replace("{" + key + "}", str(value))
        return token.replace("${SKILL_DIR}", str(self.skill_dir))
