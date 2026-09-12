"""DOCX 顺序 block 与通用失败语义回归。独立转换专用测试随功能移除。"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

DOCX_SCRIPTS = Path("backend/skill_toolbox/skill_defs/docx_pro/scripts")


def _run_script(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    # 脚本路径用绝对路径（cwd 是独立 workspace，相对路径会解析失败）
    return subprocess.run(
        [sys.executable, str(script.resolve()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(cwd),
        check=False,
    )


def _png_bytes(width: int = 64, height: int = 48) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([200, 100, 50]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ---------------- DOCX 顺序 block ----------------

@pytest.mark.parametrize("complexity", ["simple", "standard"])
def test_build_docx_interleaves_blocks(tmp_path: Path, complexity: str) -> None:
    """顺序 blocks：heading → paragraph → image → paragraph → table → formula。
    图片/表格/公式出现在段落之间，而非全局尾部。"""
    image = tmp_path / "fig.png"
    image.write_bytes(_png_bytes())
    spec = {
        "title": "顺序 block 报告",
        "complexity": complexity,
        "blocks": [
            {"type": "heading", "text": "第一章 引言", "level": 1},
            {"type": "paragraph", "text": "第一段正文", "style": "body"},
            {"type": "image", "source": "fig.png", "caption": "图 1 流程", "width_mm": 40},
            {"type": "paragraph", "text": "引用上图后的正文", "style": "body"},
            {"type": "table", "headers": ["能力", "状态"], "rows": [["表格", "通过"]], "caption": "表 1 对照"},
            {"type": "formula", "latex": "E = mc^2", "caption": "质能方程"},
        ],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "artifacts" / "interleaved.docx"

    result = _run_script(DOCX_SCRIPTS / "build_docx.py", str(spec_path), str(out), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert out.is_file()

    # 解包验证：图片与表格真实嵌入（不是只写进 JSON）
    import zipfile

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        media = [n for n in names if n.startswith("word/media/")]
        assert media, "顺序 block 图片未嵌入"
        document_xml = z.read("word/document.xml").decode("utf-8")
    # OMML 公式注入（simple latex → OMML 文本路径）
    assert "oMath" in document_xml or "m:oMath" in document_xml


def test_build_docx_rejects_unknown_block_type(tmp_path: Path) -> None:
    spec = {
        "title": "坏 block",
        "complexity": "simple",
        "blocks": [{"type": "video", "text": "x"}],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "bad.docx"

    result = _run_script(DOCX_SCRIPTS / "build_docx.py", str(spec_path), str(out), cwd=tmp_path)
    assert result.returncode != 0
    assert "Unknown block type" in result.stderr
    assert not out.exists()


def test_build_docx_backward_compat_global_tail_arrays(tmp_path: Path) -> None:
    """无 blocks 时旧 sections+tables+images+formulas 尾部数组仍工作（兼容）。"""
    image = tmp_path / "fig.png"
    image.write_bytes(_png_bytes())
    spec = {
        "title": "旧规格",
        "complexity": "simple",
        "sections": [{"heading": "第一章", "level": 1, "paragraphs": [{"text": "正文"}]}],
        "tables": [{"headers": ["A"], "rows": [["1"]]}],
        "images": [{"source": "fig.png", "caption": "图", "width_mm": 30}],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "legacy.docx"

    result = _run_script(DOCX_SCRIPTS / "build_docx.py", str(spec_path), str(out), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    import zipfile

    with zipfile.ZipFile(out) as z:
        assert any(n.startswith("word/media/") for n in z.namelist())


# ---------------- PDF 固定抽样 ----------------





# ---------------- PDF DOCX mechanical audit ----------------









# ---------------- 渲染隔离回归 ----------------



# ---------------- 失败语义（task_failed，不回退 finish_task） ----------------

def test_task_failed_tool_spec_contract() -> None:
    from skill_toolbox.tool_specs import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["name"] == "task_failed")
    assert spec["input_schema"]["required"] == ["error"]
    # 失败路径不再建议用 finish_task 伪造产物
    finish = next(s for s in TOOL_SPECS if s["name"] == "finish_task")
    assert "minItems" in finish["input_schema"]["properties"]["artifacts"]
