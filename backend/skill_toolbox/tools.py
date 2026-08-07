from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lxml import etree

from skill_toolbox.actions.docx import build_simple_docx
from skill_toolbox.models import ImageContent, ToolCall, ToolResult
from skill_toolbox.policy import PolicyViolation, WorkspacePolicy

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
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            import struct

            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return (w, h) if w and h else None
                seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
                i += 2 + seg_len
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


class ToolRegistry:
    def __init__(
        self,
        policy: WorkspacePolicy,
        allowed_actions: frozenset[str],
        skill_dir: Path = Path("."),
        scripts: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy
        self.allowed_actions = allowed_actions
        self.skill_dir = skill_dir
        self.scripts = scripts or {}
        # Extra env vars merged into exec_cmd subprocesses (e.g. MINERU_TOKEN).
        self.extra_env = env or {}
        self.actions: dict[str, Callable[[dict[str, Any]], None]] = {
            "build_simple_docx": self._build_simple_docx,
        }

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            if call.name == "read":
                return await asyncio.to_thread(self._read, call)
            if call.name == "write":
                return await asyncio.to_thread(self._write, call)
            if call.name == "edit":
                return await asyncio.to_thread(self._edit, call)
            if call.name == "exec_cmd":
                return await self._exec_cmd_async(call)
            return self._result(call, False, f"Unknown data tool: {call.name}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._result(call, False, str(exc))

    async def _exec_cmd_async(self, call: ToolCall) -> ToolResult:
        """Dispatch exec_cmd: builtins run in-process; declared scripts run as
        subprocesses with cooperative cancellation so runtime timeouts actually
        terminate the spawned process instead of leaking it."""
        action = str(call.arguments["action"])
        if action in self.actions:
            return await asyncio.to_thread(self._exec_builtin, call)
        proc_holder: dict[str, subprocess.Popen[bytes] | None] = {"proc": None}

        def runner() -> ToolResult:
            proc_holder["proc"] = subprocess.Popen(
                self._build_cmd(call),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.policy.root),
                env={**os.environ, **self.extra_env},
                close_fds=True,
            )
            try:
                stdout_b, stderr_b = proc_holder["proc"].communicate(timeout=300)
            except subprocess.TimeoutExpired:
                self._kill(proc_holder["proc"])
                return self._result(call, False, f"Script timed out: {call.arguments.get('action')}")
            rc = proc_holder["proc"].returncode or 0
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
            return await task
        except asyncio.CancelledError:
            # Runtime timed out (asyncio.wait_for) — kill the subprocess so it
            # doesn't keep consuming resources while we report the timeout.
            self._kill(proc_holder["proc"])
            raise

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _build_cmd(self, call: ToolCall) -> list[str]:
        action = str(call.arguments["action"])
        args = dict(call.arguments.get("args", {}))
        if action not in self.allowed_actions and action not in self.scripts:
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

    def _write(self, call: ToolCall) -> ToolResult:
        path = self.policy.resolve(str(call.arguments["path"]))
        content = str(call.arguments["content"])
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("write only supports text files")
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Content exceeds the 1 MB write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
        return self._result(call, True, f"Wrote {path.relative_to(self.policy.root)}")

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

    def _exec_builtin(self, call: ToolCall) -> ToolResult:
        """Handle in-process builtin actions (e.g. build_simple_docx)."""
        action = str(call.arguments["action"])
        args = dict(call.arguments.get("args", {}))
        if action not in self.allowed_actions and action not in self.scripts:
            raise ValueError(f"Action is not allowed: {action}")
        builtin = self.actions.get(action)
        if builtin is None:
            raise ValueError(
                f"Action {action} is not a builtin; it must run via the subprocess path"
            )
        builtin(args)
        return self._result(call, True, f"Action completed: {action}")

    def _render(self, token: str, args: dict[str, Any]) -> str:
        for key, value in args.items():
            token = token.replace("{" + key + "}", str(value))
        return token.replace("${SKILL_DIR}", str(self.skill_dir))

    def _build_simple_docx(self, args: dict[str, Any]) -> None:
        spec_path = self.policy.require_file(str(args["spec_path"]))
        output_path = self.policy.resolve(str(args["output_path"]))
        build_simple_docx(spec_path, output_path)
