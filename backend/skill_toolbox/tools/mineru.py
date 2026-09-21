"""MinerU 适配与跨任务持久缓存（实施计划 §6）。

Cache key 至少包含：PDF SHA256、MinerU/adapter 版本、转换参数 hash、
语言、模型/模式（§6.1）。完整命中禁止调用 MinerU；未命中成功后原子提交；
损坏/过期/不完整按 miss 处理。Token 只在内存与受控子进程环境传递，
永不写入缓存、metadata 或日志。

runner 协议：``run(command, *, timeout) -> subprocess.CompletedProcess[bytes]``，
由 MaterialService 注入（负责 MINERU_TOKEN 与取消语义）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.tools.cache import HashCache
from skill_toolbox.tools.workspace import sha256_file
from skill_toolbox.unicode_utils import redact_secrets


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

MINERU_ADAPTER_VERSION = "1"

# 转换失败稳定错误码（与 materials.MINERU_FATAL_CODES 对齐）
MINERU_PARSE_FAILED = "MINERU_PARSE_FAILED"
MINERU_TOKEN_MISSING = "MINERU_TOKEN_MISSING"
MINERU_CLI_MISSING = "MINERU_CLI_MISSING"


@dataclass(frozen=True)
class MineruOptions:
    """convert_pdf 契约参数（model/ocr/formula/table/language + 可选 pages）。

    任一参数变化都会改变 options_hash，从而产生不同缓存项（§6.1）。
    """

    model: str = "auto"
    ocr: bool = True
    formula: bool = True
    table: bool = True
    language: str = "ch"
    pages: str | None = None  # "1-3,5" 或 None=全文（convert_pdf 不传 --pages）

    def as_args(self) -> list[str]:
        return [
            self.model,
            str(self.ocr).lower(),
            str(self.formula).lower(),
            str(self.table).lower(),
            self.language,
        ]

    def options_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def default_cache_root() -> Path:
    """缓存根目录（计划 §6.2：<app-data>/skill-toolbox-cache）。

    测试可通过环境变量 SKILL_TOOLBOX_CACHE_DIR 或构造参数覆盖。
    """
    override = os.environ.get("SKILL_TOOLBOX_CACHE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        )
        return base / "skill-toolbox-cache"
    return Path.home() / ".cache" / "skill-toolbox-cache"


def mineru_cli_version() -> str:
    """探测 mineru-open-api 版本，用于 cache key（解析器升级后旧缓存失效）。

    npm 全局安装时读包内 package.json 的 version；单文件二进制用 mtime 近似
    版本变化；探测失败返回 "unknown"（不阻断转换，仅缓存项按 unknown 分组）。
    """
    try:
        cli = resolve_mineru_cli()
    except RuntimeError:
        return "cli-missing"
    for token in cli:
        lower = token.lower()
        if lower.endswith((".cmd", ".bat")):
            continue
        candidate = Path(token)
        # node + <npm>/bin/mineru-open-api → 包目录在 bin 的父级
        pkg = candidate.parent.parent / "package.json"
        if pkg.is_file():
            try:
                version = json.loads(pkg.read_text(encoding="utf-8")).get("version")
                return str(version) if version else "unknown"
            except (OSError, ValueError):
                return "unknown"
        try:
            return f"bin-{int(candidate.stat().st_mtime_ns)}"
        except OSError:
            return "unknown"
    return "unknown"


def _default_runner() -> Callable[[list[str], dict[str, Any]], subprocess.CompletedProcess[bytes]]:
    def run(
        command: list[str], *, timeout: float | None = None, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False, **kwargs)

    return run


class MineruService:
    """跨任务持久缓存 + convert_pdf 子进程适配。

    缓存布局：<cache_root>/mineru/<key>/（data/ + metadata.json + COMPLETE）。
    命中/未命中按 §6.3：完整命中复制结果到目标 DOCX 并返回 cache_hit，
    禁止调用 MinerU；未命中才运行 convert_pdf，成功审计后原子提交。
    """

    def __init__(
        self,
        cache_root: Path,
        convert_script: Path,
        runner: Callable[[list[str], dict[str, Any]], subprocess.CompletedProcess[bytes]]
        | None = None,
        timeout: float = 1860.0,
    ) -> None:
        self.cache = HashCache(cache_root / "mineru")
        self.convert_script = convert_script
        self.runner = runner or _default_runner()
        self.timeout = timeout

    # ---------------- cache key（§6.1） ----------------

    def cache_key(self, pdf_sha256: str, options: MineruOptions) -> str:
        parts = [
            "adapter:" + MINERU_ADAPTER_VERSION,
            "cli:" + mineru_cli_version(),
            "pdf:" + pdf_sha256,
            "options:" + options.options_hash(),
            "language:" + options.language,
            "model:" + options.model,
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    # ---------------- 完整性校验（§6.3 第 4 步） ----------------

    def _validator(self, data_dir: Path, metadata: dict[str, Any]) -> None:
        """缓存产物必须可读：result.docx 是合法 OOXML 包，result.json 存在。

        metadata 校验版本兼容：adapter_version 必须与当前一致（旧 adapter 的
        缓存按 miss 处理，不覆盖其它 key）。
        """
        if metadata.get("adapter_version") != MINERU_ADAPTER_VERSION:
            raise ValueError("cache adapter version mismatch")
        docx = data_dir / "result.docx"
        if not docx.is_file() or docx.stat().st_size == 0:
            raise ValueError("cached docx missing or empty")
        try:
            with zipfile.ZipFile(docx) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("cached docx is not a readable OOXML package")
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError(f"cached docx unreadable: {exc}") from None
        if not (data_dir / "result.json").is_file():
            raise ValueError("cached result.json missing")

    # ---------------- 转换主流程（§6.3） ----------------

    def convert(
        self, pdf: Path, out_docx: Path, options: MineruOptions | None = None
    ) -> dict[str, Any]:
        """将 PDF/DOCX 源文档经 MinerU 解析成 DOCX，带跨任务持久缓存。

        返回 {"status": "cache_hit"|"cache_miss", "output", "cache_key",
        "metadata"}。命中时输出已复制到 out_docx；未命中成功时缓存已提交。
        失败抛 ToolError（稳定 code），临时目录已清理，不生成 COMPLETE。
        """
        options = options or MineruOptions()
        if not self.convert_script.is_file():
            raise ToolError(
                MINERU_PARSE_FAILED,
                f"convert_pdf 脚本缺失: {self.convert_script}",
                retryable=False,
            )
        pdf_sha256 = sha256_file(pdf)
        key = self.cache_key(pdf_sha256, options)
        out_docx.parent.mkdir(parents=True, exist_ok=True)

        with self.cache.lock(key):
            metadata = self.cache.lookup(key, self._validator)
            if metadata is not None:
                # 完整命中：复制结果到目标 DOCX，禁止调用 MinerU
                shutil.copy2(self.cache.data_dir(key) / "result.docx", out_docx)
                return {
                    "status": "cache_hit",
                    "output": str(out_docx),
                    "cache_key": key,
                    "metadata": metadata,
                }

            tmp, _meta_path = self.cache.stage(key)
            try:
                result_docx = tmp / "result.docx"
                completed = self.runner(
                    [
                        sys.executable,
                        str(self.convert_script),
                        str(pdf),
                        str(result_docx),
                        *options.as_args(),
                        "--internal-absolute-paths",
                    ],
                    timeout=self.timeout,
                )
                if completed.returncode or not result_docx.is_file():
                    detail = (
                        completed.stderr.decode("utf-8", errors="replace")[-2000:]
                        or completed.stdout.decode("utf-8", errors="replace")[-2000:]
                    )
                    detail = redact_secrets(detail)
                    code = MINERU_PARSE_FAILED
                    if any(k in detail.lower() for k in ("token", "auth", "401")):
                        code = MINERU_TOKEN_MISSING
                    raise ToolError(code, detail or "MinerU 转换失败")
                metadata = {
                    "adapter_version": MINERU_ADAPTER_VERSION,
                    "cli_version": mineru_cli_version(),
                    "pdf_sha256": pdf_sha256,
                    "model": options.model,
                    "language": options.language,
                    "options": asdict(options),
                    "created_at": _now_iso(),
                }
                # 转换结果元数据（§6.2 布局 result.json）+ 提交前审计
                (tmp / "result.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._validator(tmp, metadata)
                self.cache.commit(key, tmp, metadata)
                shutil.copy2(self.cache.data_dir(key) / "result.docx", out_docx)
            except BaseException:
                # 失败/取消只清理临时目录，不生成 COMPLETE（§6.3 第 7 步）
                self.cache.cleanup_tmp(key)
                raise

        return {
            "status": "cache_miss",
            "output": str(out_docx),
            "cache_key": key,
            "metadata": metadata,
        }


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()
