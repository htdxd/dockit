from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def workspace_path(value: str) -> Path:
    root = Path.cwd().resolve()
    path = (root / value).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(value)
    return path


def run_text(command: list[str]) -> str:
    # text=True 在中文 Windows 上用 GBK 解码 poppler 的 UTF-8 输出会抛
    # UnicodeDecodeError；改为 bytes 捕获 + UTF-8 容错解码。
    try:
        proc = subprocess.run(command, capture_output=True, timeout=30, check=False)
        return proc.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def main() -> None:
    source = workspace_path(sys.argv[1])
    fonts = run_text(["pdffonts", str(source)])
    font_rows = [line for line in fonts.splitlines()[2:] if line.strip()]
    page_count = _count_pages(source)
    samples = _sample_pages(page_count)
    # 固定抽样首/中/末（最多 3 页），不能只凭第一页决定整份文档
    # （实施计划 §10.3）。样本页逐页抽取文本字符数。
    per_page: dict[int, int] = {}
    for page in samples:
        extracted = run_text(
            ["pdftotext", "-f", str(page), "-l", str(page), str(source), "-"]
        )
        per_page[page] = len(extracted.strip())
    sampled_chars = sum(per_page.values())
    first_chars = per_page.get(1, 0)
    sampled_pages = len(samples)
    # 全样本都无文本才算扫描件；native 判定要求"首页有实质文本，且样本中
    # 至少两页有正文"——首页有字但中/末页为空的稀疏文档归为 mixed，不能只信
    # 首页（实施计划 §10.3 固定抽样：首/中/末都要参与判定）。
    text_pages = sum(1 for v in per_page.values() if v >= 40)
    if not font_rows and sampled_chars < 40:
        classification = "scanned"
    elif font_rows and first_chars >= 100 and text_pages >= min(2, sampled_pages):
        classification = "native_structured"
    else:
        classification = "mixed_or_uncertain"

    print(json.dumps({
        "source": str(source.relative_to(Path.cwd())),
        "classification": classification,
        "font_rows": len(font_rows),
        "first_page_text_characters": first_chars,
        "sampled_text_characters": sampled_chars,
        "page_count": page_count,
        "sample_pages": samples,
        "per_page_characters": per_page,
    }, ensure_ascii=False))


def _count_pages(path: Path) -> int | None:
    """尝试用 pdfinfo 获取总页数；失败返回 None（保持简单，不逐页解析）。"""
    try:
        proc = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=30, check=False)
        out = proc.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _sample_pages(page_count: int | None) -> list[int]:
    """固定最多 3 页样本：首、中、末（实施计划 §10.3 固定抽样）。

    不能只凭第一页决定整份文档；长文档抽样首/中/末，短文档取全部。
    """
    if not page_count or page_count <= 0:
        return [1]
    if page_count <= 3:
        return list(range(1, page_count + 1))
    middle = page_count // 2
    sample = [1, middle, page_count]
    return sorted({p for p in sample if 1 <= p <= page_count})


if __name__ == "__main__":
    main()
