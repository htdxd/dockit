from __future__ import annotations

# Internal PDF material adapter; not an agent-visible conversion feature.

import json
import shutil
import subprocess
import sys
from pathlib import Path

INTERNAL_ABSOLUTE_FLAG = "--internal-absolute-paths"


def workspace_path(value: str, must_exist: bool, *, allow_absolute: bool = False) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    if not allow_absolute:
        path.relative_to(root)
    if must_exist and not path.is_file():
        raise FileNotFoundError(value)
    return path


def boolean(value: str) -> str:
    normalized = value.lower()
    if normalized not in {"true", "false"}:
        raise ValueError("Boolean values must be true or false")
    return normalized


def mineru_command() -> list[str]:
    """解析 mineru-open-api 的可执行入口，返回 argv 前缀（不含子命令）。

    npm 全局安装的 mineru-open-api 在 Windows 上是 `mineru-open-api.cmd`
    包装器，Python 的 subprocess 不带 shell 时无法直接执行 .cmd/.bat
    （CreateProcess 不按 PATHEXT 解析）→ FileNotFoundError。
    这里优先解析到 node + 包内 JS 入口直接调用；找不到 node 时退化为
    `cmd.exe /c`（此时按字符串拼接命令，路径中不要含引号）。
    """
    cli = shutil.which("mineru-open-api")
    if not cli:
        raise RuntimeError(
            "未找到 mineru-open-api，无法调用 MinerU 转换。请先安装并加入 PATH："
            "npm install -g mineru-open-api（或在设置页确认 MinerU Token 已填写）。"
        )
    cli_path = Path(cli)
    if cli_path.suffix.lower() in {".cmd", ".bat"}:
        js_bin = cli_path.parent / "node_modules" / "mineru-open-api" / "bin" / "mineru-open-api"
        node = shutil.which("node")
        if node and js_bin.is_file():
            return [node, str(js_bin)]
        return ["cmd.exe", "/c", str(cli_path)]
    return [str(cli_path)]


def main() -> None:
    raw_args = sys.argv[1:]
    allow_absolute = bool(raw_args and raw_args[-1] == INTERNAL_ABSOLUTE_FLAG)
    if allow_absolute:
        raw_args = raw_args[:-1]
    if len(raw_args) < 2:
        raise ValueError("convert_pdf 缺少 source/output 参数")
    source = workspace_path(raw_args[0], must_exist=True, allow_absolute=allow_absolute)
    output = workspace_path(raw_args[1], must_exist=False, allow_absolute=allow_absolute)
    args = raw_args[2:]
    # manifest argv 契约：model/ocr/formula/table/language 五项，默认处理全文。
    # 早期契约曾含 {pages} 作为第 6 项（已移除）；若仍收到 6 项视为旧式调用，
    # 丢弃末尾 pages 即可，不要把它传给 MinerU。
    if len(args) == 6:
        args = args[:5]
    if len(args) != 5:
        raise ValueError(
            f"convert_pdf 需要 5 个参数（model/ocr/formula/table/language），"
            f"实际收到 {len(args)} 个: {args!r}"
        )
    if any("{" in v or "}" in v for v in args):
        raise ValueError(
            f"convert_pdf 收到未渲染的占位符参数（参数缺漏）: {args!r}，请按 manifest 补齐"
        )
    model, ocr, formula, table, language = args
    output.parent.mkdir(parents=True, exist_ok=True)

    command = mineru_command() + [
        "extract", str(source), "-o", str(output.parent), "-f", "docx",
        "--model", model, "--ocr=" + boolean(ocr), "--formula=" + boolean(formula),
        "--table=" + boolean(table), "--language", language, "--timeout", "1800",
    ]
    # 默认处理全文：不传 --pages。

    # 用 bytes 捕获输出：中文 Windows 的默认编码是 GBK，text=True 会让
    # subprocess 的 _readerthread 用 GBK 解码 MinerU 的 UTF-8 输出并崩溃
    # （UnicodeDecodeError），导致 stderr 为 None、后续切片报 TypeError。
    completed = subprocess.run(command, capture_output=True, timeout=1860, check=False)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    docx_files = sorted(output.parent.glob("*.docx"))
    if completed.returncode or not docx_files:
        detail = stderr[-2000:] or stdout[-2000:] or "MinerU did not create a DOCX"
        hint = ""
        if "token" in detail.lower() or "auth" in detail.lower() or "401" in detail:
            hint = "（提示：请到设置页「第三方服务」填写 MinerU API Token）"
        raise RuntimeError(detail + hint)

    # 多 PDF 同任务时，输出目录是本次调用专属的 material/artifact 子目录
    # （由 render_docx 与 runtime 隔离保证）；这里仍必须选择"本次调用明确
    # 返回"的 DOCX：优先取与源文件同 stem 的产物（MinerU 单文件转换约定），
    # 找不到再退回目录中修改时间最新的 docx。禁止 glob 共享目录里第一个
    # .docx 就交付（实施计划 §10.3）。
    stem = source.stem
    generated = next((f for f in docx_files if f.stem == stem), None)
    if generated is None:
        generated = max(docx_files, key=lambda f: f.stat().st_mtime)
    if generated.resolve() != output.resolve():
        generated.replace(output)
    # output 可能不在 cwd 下（共享解析用临时 workspace）；能相对化才相对化
    try:
        out_rel = str(output.relative_to(Path.cwd()))
    except ValueError:
        out_rel = str(output)
    print(json.dumps({"output": out_rel, "model": model}, ensure_ascii=False))


if __name__ == "__main__":
    main()
