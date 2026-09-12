"""download_assets.py — 把 md 材料里的远端图片下载到工作区，供 build_docx 嵌入。

Usage: python download_assets.py <source_md>
  source_md: 材料 md 的相对路径（work/materials/<id>/content.md 或 sources/...），
             脚本从中提取 `![..](url)` / `<img src=url>` 的 http(s) 图片地址。

输出（stdout，JSON）：
  {"ok": true, "downloaded": [{"path": "work/assets/<hash>-<name>"}, ...],
   "failed": [{"url": ..., "error": ...}], "total": N}
  只回 path 不回完整 url（返回内容多会被截断，模型拿不全 path 就会自造）。
  模型用返回的 path 作为 spec.json image block 的 source（如
  {"type": "image", "source": "work/assets/abc12345-fig1.png", "caption": "..."}）。

设计要点：
- 只下载材料自身引用的 http(s) 图片；data:/file: 忽略。
- 每个请求超时、限大小，单个失败不中断其余（记入 failed）。
- 产物写 cwd/work/assets/（cwd = 工作区根，与其它脚本一致），文件名带内容
  hash 前缀避免重名/重复下载。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

# 与 materials.py 的引用提取一致：markdown 图片 + <img src>
_MD_IMG_RE = re.compile(
    r"!\[[^\]]*\]\(([^\s)\"]+)(?:\s+[\"'(].*?)?\)|<img[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

_DOWNLOAD_TIMEOUT = 20  # 单请求秒
_MAX_BYTES = 8 * 1024 * 1024  # 单图上限 8MB
_MAX_IMAGES = 60  # 最多下载 60 张，防滥用


def _extract_urls(md_text: str) -> list[str]:
    urls: list[str] = []
    for m in _MD_IMG_RE.finditer(md_text):
        raw = m.group(1) or m.group(2) or ""
        raw = raw.split("#")[0].split("?")[0] if not raw.startswith("data:") else raw
        if raw.startswith(("http://", "https://")):
            urls.append(raw)
    # 去重保序
    seen: set[str] = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _download_one(url: str, assets_dir: Path) -> tuple[str | None, str | None]:
    """下载单张图片到 assets_dir；返回 (相对 path, 错误)。"""
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    if not name or "." not in name:
        name = "image.png"
    safe_name = "".join(c for c in name if c.isalnum() or c in "._-").strip(".")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            data = resp.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return None, f"超过 {_MAX_BYTES} 字节上限"
    except Exception as exc:  # noqa: BLE001 - 单图失败不中断
        return None, f"{type(exc).__name__}: {exc}"
    digest = hashlib.sha256(data).hexdigest()[:8]
    out = assets_dir / f"{digest}-{safe_name or 'image'}"
    out.write_bytes(data)
    return f"work/assets/{out.name}", None


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "缺少 source_md 参数"}, ensure_ascii=False))
        return 2
    md_rel = sys.argv[1]
    md_path = Path(md_rel)
    if not md_path.is_file():
        md_path = Path("sources") / md_rel
    if not md_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"找不到材料文件: {md_rel}"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        md_text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"读取失败: {exc}"}, ensure_ascii=False))
        return 2

    urls = _extract_urls(md_text)
    if not urls:
        print(
            json.dumps(
                {"ok": True, "downloaded": [], "failed": [], "total": 0,
                 "hint": "材料里没有 http(s) 图片引用，无需下载"},
                ensure_ascii=False,
            )
        )
        return 0

    assets_dir = Path("work") / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict] = []
    failed: list[dict] = []
    for url in urls[:_MAX_IMAGES]:
        path, err = _download_one(url, assets_dir)
        if err is None and path:
            downloaded.append({"path": path})
        else:
            failed.append({"url": url[:80], "error": err or "未知错误"})

    print(
        json.dumps(
            {
                "ok": True,
                "downloaded": downloaded,
                "failed": failed,
                "total": len(downloaded) + len(failed),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
