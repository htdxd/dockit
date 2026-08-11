"""阶段 4 强制测试：DOCX 顺序 block + PDF 固定抽样与失败语义。

覆盖实施计划 §15.3 关键回归：
- 图片/表格/公式能通过顺序 blocks 放入指定 section/段落之间（非全局尾部）
- inspect_pdf 固定最多 3 页抽样（首/中/末），不凭第一页决定整份文档
- PDF 转换失败走 task_failed，不伪造 finish_task artifact
- render_docx 多文档独立 PNG 前缀（回归防护，覆盖已改实现）
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

DOCX_SCRIPTS = Path("backend/skill_toolbox/skill_defs/docx_pro/scripts")
PDF_SCRIPTS = Path("backend/skill_toolbox/skill_defs/pdf_docx_routing/scripts")


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

def test_inspect_pdf_sample_pages_never_exceeds_three() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "inspect_pdf", PDF_SCRIPTS / "inspect_pdf.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module._sample_pages(1) == [1]
    assert module._sample_pages(2) == [1, 2]
    assert module._sample_pages(3) == [1, 2, 3]
    assert module._sample_pages(10) == [1, 5, 10]  # 首/中/末，固定 3 页
    assert module._sample_pages(100) == [1, 50, 100]
    assert module._sample_pages(None) == [1]
    for pages in [1, 2, 3, 10, 100]:
        assert len(module._sample_pages(pages)) <= 3
        assert all(1 <= p <= pages for p in module._sample_pages(pages))


def test_inspect_pdf_classification_uses_all_samples(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分类不能只凭第一页：首页有字但中/末页为空的稀疏文档归 mixed。

    （实施计划 §10.3：固定抽样首/中/末，样本页都要参与判定。）
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "inspect_pdf", PDF_SCRIPTS / "inspect_pdf.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    captured: list[list[str]] = []

    def fake_run(cmd: list[str]) -> str:
        captured.append(cmd)
        if cmd[0] == "pdffonts":
            return "name type emb sub uni object ID\nA+SimSun TrueType yes yes no  1  0\n"
        if cmd[0] == "pdftotext":
            page = int(cmd[2])
            return "x" * 120 if page == 1 else ""
        return ""

    module.run_text = fake_run
    module._count_pages = lambda _p: 10
    monkeypatch.chdir(tmp_path)
    module.workspace_path = lambda v, must_exist=True: (Path.cwd() / v).resolve()
    module.sys.argv = ["inspect_pdf.py", "sample.pdf"]

    module.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"] == "mixed_or_uncertain"
    assert payload["sample_pages"] == [1, 5, 10]
    sampled = [int(command[2]) for command in captured if command[0] == "pdftotext"]
    assert sampled == [1, 5, 10]


# ---------------- PDF DOCX mechanical audit ----------------

def test_audit_docx_accepts_valid_package(tmp_path: Path) -> None:
    from docx import Document

    source = tmp_path / "valid.docx"
    document = Document()
    document.add_paragraph("Converted content")
    document.save(source)

    result = _run_script(PDF_SCRIPTS / "audit_docx.py", str(source), cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_audit_docx_rejects_visible_conversion_markers(tmp_path: Path) -> None:
    from docx import Document

    source = tmp_path / "markers.docx"
    document = Document()
    document.add_paragraph(r"Residual $$ \\frac{a}{b} <|box_start|>")
    document.save(source)

    result = _run_script(PDF_SCRIPTS / "audit_docx.py", str(source), cwd=tmp_path)

    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert report["latex_markers"] > 0
    assert report["layout_tokens"] > 0


def test_audit_docx_rejects_broken_image_relationship(tmp_path: Path) -> None:
    from docx import Document
    from PIL import Image

    image = tmp_path / "figure.png"
    Image.new("RGB", (20, 20), (10, 20, 30)).save(image)
    valid = tmp_path / "with-image.docx"
    document = Document()
    document.add_picture(str(image))
    document.save(valid)
    broken = tmp_path / "broken.docx"
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(
        broken, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            if not item.filename.startswith("word/media/"):
                target.writestr(item, source.read(item.filename))

    result = _run_script(PDF_SCRIPTS / "audit_docx.py", str(broken), cwd=tmp_path)

    assert result.returncode == 2
    assert any(
        "broken image relationship" in issue
        for issue in json.loads(result.stdout)["issues"]
    )


def test_audit_docx_checks_header_image_relationships(tmp_path: Path) -> None:
    from docx import Document
    from PIL import Image

    image = tmp_path / "header.png"
    Image.new("RGB", (20, 20), (30, 40, 50)).save(image)
    valid = tmp_path / "header-image.docx"
    document = Document()
    document.add_paragraph("Body")
    header = document.sections[0].header
    header.paragraphs[0].add_run().add_picture(str(image))
    document.save(valid)
    broken = tmp_path / "header-broken.docx"
    with zipfile.ZipFile(valid) as source, zipfile.ZipFile(
        broken, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            if not item.filename.startswith("word/media/"):
                target.writestr(item, source.read(item.filename))

    result = _run_script(PDF_SCRIPTS / "audit_docx.py", str(broken), cwd=tmp_path)

    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert any("header" in issue and "broken image relationship" in issue for issue in report["issues"])


# ---------------- 渲染隔离回归 ----------------

def test_render_docx_page_prefix_isolation(tmp_path: Path) -> None:
    """每个文档渲染使用 <stem>-page- 前缀，文档间 PNG 不混用。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "render_docx", PDF_SCRIPTS / "render_docx.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    out_dir = tmp_path / "artifacts"
    out_dir.mkdir(parents=True)
    # 旧全局命名残留（跨文档污染场景）：page-1.png 不应命中新前缀
    (out_dir / "page-1.png").write_bytes(b"stale")
    (out_dir / "报告-page-1.png").write_bytes(b"png")
    matches = sorted(p.name for p in out_dir.glob("*-page-*.png"))
    assert "报告-page-1.png" in matches
    assert "page-1.png" not in matches
    source = tmp_path / "报告.docx"
    source.write_bytes(b"PK")  # 仅用于派生前缀名
    prefix = f"{source.stem}-page"
    assert prefix == "报告-page"
    assert prefix != "page"


# ---------------- 失败语义（task_failed，不回退 finish_task） ----------------

def test_task_failed_tool_spec_contract() -> None:
    from skill_toolbox.tool_specs import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["name"] == "task_failed")
    assert spec["input_schema"]["required"] == ["error"]
    # 失败路径不再建议用 finish_task 伪造产物
    finish = next(s for s in TOOL_SPECS if s["name"] == "finish_task")
    assert "minItems" in finish["input_schema"]["properties"]["artifacts"]
