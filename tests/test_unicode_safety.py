from pathlib import Path

import pytest
from skill_toolbox.models import ToolCall
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.tools import ToolRegistry

import asyncio
import json
import re


@pytest.mark.asyncio
async def test_tool_call_replaces_unpaired_surrogate_before_write(
    tmp_path: Path,
) -> None:
    call = ToolCall(
        id="write-invalid-unicode",
        name="write",
        arguments={"path": "work/content.txt", "content": "before\udcafafter"},
    )
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())

    result = await registry.execute(call)

    assert result.success is True
    assert (tmp_path / "work" / "content.txt").read_text(encoding="utf-8") == (
        "before\ufffdafter"
    )


def _make_script_skill(tmp_path: Path) -> ToolRegistry:
    """Skill with one echo script whose argv has two placeholders + a literal."""
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "echo.py").write_text(
        "import sys\nprint(sys.argv)\n", encoding="utf-8"
    )
    return ToolRegistry(
        WorkspacePolicy(tmp_path / "workspace"),
        frozenset(),
        skill_dir=skill_dir,
        scripts={"echo": ("echo.py", ("{a}", "{b}", "literal"))},
    )


def test_build_cmd_rejects_unrendered_placeholder(tmp_path: Path) -> None:
    """A model that omits a required arg must not leak the raw {placeholder}
    into the script command line (the pdf_docx 20-step-burnout root cause)."""
    registry = _make_script_skill(tmp_path)
    call = ToolCall(
        id="missing-b",
        name="exec_cmd",
        arguments={"action": "echo", "args": {"a": "1"}},
    )
    with pytest.raises(ValueError, match="缺少必要参数|unrendered|placeholder"):
        registry._build_cmd(call)


def test_build_cmd_renders_full_args(tmp_path: Path) -> None:
    registry = _make_script_skill(tmp_path)
    call = ToolCall(
        id="full",
        name="exec_cmd",
        arguments={"action": "echo", "args": {"a": "1", "b": "2"}},
    )
    cmd = registry._build_cmd(call)
    assert cmd[-3:] == ["1", "2", "literal"]


def test_build_cmd_allows_empty_string_value(tmp_path: Path) -> None:
    """An explicitly-empty value is a valid answer, not a missing placeholder."""
    registry = _make_script_skill(tmp_path)
    call = ToolCall(
        id="empty-a",
        name="exec_cmd",
        arguments={"action": "echo", "args": {"a": "", "b": "2"}},
    )
    cmd = registry._build_cmd(call)
    assert cmd[-3:] == ["", "2", "literal"]


def _make_simple_docx(path: Path, paragraphs: list[str]) -> None:
    """Build a minimal valid .docx (document.xml with w:p/w:t)."""
    import zipfile

    ns = "xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'"
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    doc = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f"<w:document {ns}><w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)


@pytest.mark.asyncio
async def test_read_docx_returns_text_view(tmp_path: Path) -> None:
    """read on a .docx material returns an extracted plain-text view."""
    source = tmp_path / "old_resume.docx"
    _make_simple_docx(source, ["姓 名：张三", "电 话：138-1234", "工作经历", "AI 算法工程师 某公司 2021-至今"])
    call = ToolCall(
        id="read-docx",
        name="read",
        arguments={"path": source.name},
    )
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())

    result = await registry.execute(call)

    assert result.success is True
    payload = json.loads(result.content)
    assert payload["format"] == "docx-text"
    assert "姓 名：张三" in payload["content"]
    assert "工作经历" in payload["content"]


def _png_bytes(w: int, h: int) -> bytes:
    import struct

    def chunk(t: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", 0)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = b"x\x9c\x01\x00\x00\xff\x00\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _jpeg_bytes(w: int, h: int) -> bytes:
    """最小合法 JPEG（SOI + SOF0 + EOI），SOF0 长度 17，标准三段式色度采样。"""
    import struct

    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)  # 段长 = 8 + 3*components
        + b"\x08"  # precision
        + struct.pack(">HH", h, w)
        + b"\x03"
        + b"\x01\x22\x00" + b"\x02\x11\x01" + b"\x03\x11\x01"
    )
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


_DOCX_NS = (
    "xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main' "
    "xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships' "
    "xmlns:wp='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing' "
    "xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main' "
    "xmlns:pic='http://schemas.openxmlformats.org/drawingml/2006/picture'"
)


def _docx_with_images(path: Path, images: list[tuple[str, str, bytes]]) -> None:
    """Build a .docx embedding images via r:embed. images = [(rid, rel_target, bytes)]，
    引用顺序即列表顺序；rel_target 为 rels 里的 Target（如 media/photo.png）。"""
    import zipfile

    def inline(rid: str) -> str:
        return (
            "<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>"
            "<pic:pic><pic:blipFill><a:blip r:embed=\"" + rid + "\"/></pic:blipFill>"
            "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        )

    doc = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f"<w:document {_DOCX_NS}><w:body>{''.join(inline(rid) for rid, _, _ in images)}</w:body></w:document>"
    )
    rels_body = "".join(
        f"<Relationship Id='{rid}' Target='{tgt}' "
        "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'/>"
        for rid, tgt, _ in images
    )
    rels = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        + rels_body
        + "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        for rid, tgt, raw in images:
            z.writestr("word/" + tgt, raw)


def _make_photo_docx(path: Path, photo_wh: tuple[int, int], logo_wh: tuple[int, int]) -> None:
    """Build a .docx embedding two images via r:embed (photo first, logo second)."""
    _docx_with_images(
        path,
        [
            ("rId100", "media/photo.png", _png_bytes(*photo_wh)),
            ("rId200", "media/logo.png", _png_bytes(*logo_wh)),
        ],
    )


@pytest.mark.asyncio
async def test_read_docx_reports_media_with_portrait_heuristic(tmp_path: Path) -> None:
    """read .docx returns a media list with portrait_likely so a non-vision
    model can pick the headshot without looking at the image."""
    from skill_toolbox.tools import _extract_docx_media

    source = tmp_path / "old_resume.docx"
    _make_photo_docx(source, (220, 260), (600, 40))
    call = ToolCall(
        id="read-docx-media",
        name="read",
        arguments={"path": source.name},
    )
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())

    result = await registry.execute(call)

    assert result.success is True
    payload = json.loads(result.content)
    media = payload["media"]
    assert len(media) == 2
    # 按引用顺序排序：photo 在前，logo 在后
    assert media[0]["name"] == "photo.png"
    assert media[1]["name"] == "logo.png"
    # 人像启发式
    assert media[0]["portrait_likely"] is True
    assert media[1]["portrait_likely"] is False
    # 物化到 work/_media/<stem>/，模型可直接引用该相对路径
    assert (tmp_path / "work" / "_media" / "old_resume" / "photo.png").is_file()
    # 媒体路径是工作区相对路径，可被后续 read/fill 使用
    assert str(media[0]["path"]).replace("\\", "/").startswith("work/_media/old_resume/")


def test_edit_rejects_json_corruption(tmp_path: Path) -> None:
    """edit on a .json target validates the result; corrupting edits are rejected
    instead of silently breaking resume_data.json."""
    import json as _json

    target = tmp_path / "work" / "resume_data.json"
    target.parent.mkdir()
    target.write_text('{"fields": {"name": "张三"}}', encoding="utf-8")
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())
    call = ToolCall(
        id="edit-json",
        name="edit",
        arguments={
            "path": "work/resume_data.json",
            "old_text": '"张三"',
            "new_text": '"张三",',  # 制造尾逗号 → 非法 JSON
        },
    )

    result = asyncio.run(registry.execute(call))

    assert result.success is False
    assert "JSON" in result.content
    assert "write" in result.content
    # 文件保持原样，未被半写入
    assert _json.loads(target.read_text(encoding="utf-8")) == {"fields": {"name": "张三"}}  # type: ignore[arg-type]


def test_edit_allows_valid_json_change(tmp_path: Path) -> None:
    target = tmp_path / "work" / "resume_data.json"
    target.parent.mkdir()
    target.write_text('{"fields": {"name": "张三"}}', encoding="utf-8")
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())
    call = ToolCall(
        id="edit-json-ok",
        name="edit",
        arguments={
            "path": "work/resume_data.json",
            "old_text": '"张三"',
            "new_text": '"李四"',
        },
    )

    result = asyncio.run(registry.execute(call))

    assert result.success is True
    import json as _json

    assert _json.loads(target.read_text(encoding="utf-8")) == {"fields": {"name": "李四"}}  # type: ignore[arg-type]


def test_fill_resume_remove_photo(tmp_path: Path) -> None:
    """fill_resume with photo=__remove__ removes the template photo drawing and
    drops the old media member from the output docx (no dangling image bytes)."""
    import json as _json
    import subprocess
    import sys
    import zipfile

    skill_dir = (
        Path(__file__).resolve().parents[1]
        / "backend" / "skill_toolbox" / "skill_defs" / "resume_pro"
    )
    script = skill_dir / "scripts" / "fill_resume.py"
    tpl_dir = skill_dir / "templates" / "t001"
    assert script.is_file() and (tpl_dir / "template.docx").is_file(), "resume_pro skill files missing"

    with zipfile.ZipFile(tpl_dir / "template.docx") as z:
        media_names = [n for n in z.namelist() if n.startswith("word/media/") and not n.endswith("/")]
    assert media_names, "template t001 should embed at least one media member"

    workspace = tmp_path
    data = {"fields": {"photo": "__remove__", "name": "张三"}}
    data_path = workspace / "data.json"
    data_path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out_path = workspace / "artifacts" / "resume.docx"

    completed = subprocess.run(
        [sys.executable, str(script), str(tpl_dir), str(data_path), str(out_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(workspace),
    )
    assert completed.returncode == 0, completed.stderr
    payload = _json.loads(completed.stdout)
    assert any(r["id"] == "photo" for r in payload["replaced"])

    with zipfile.ZipFile(out_path) as z:
        out_names = z.namelist()
        doc_xml = z.read("word/document.xml").decode("utf-8")
    assert "rId4" not in doc_xml, "photo drawing should be removed from document.xml"
    assert not any(n in out_names for n in media_names), (
        f"old media {media_names} should be dropped from output"
    )


def test_image_size_parses_jpeg_and_png(tmp_path: Path) -> None:
    """JPEG SOF / PNG IHDR 尺寸解析——回归：import struct 曾只写在 PNG 分支内，
    导致任何 JPEG 都抛 UnboundLocalError，read 含照片的旧简历 docx 整体失败。"""
    from skill_toolbox.tools import _image_size

    jpg = tmp_path / "photo.jpg"
    jpg.write_bytes(_jpeg_bytes(300, 400))
    assert _image_size(jpg) == (300, 400)
    png = tmp_path / "photo.png"
    png.write_bytes(_png_bytes(220, 260))
    assert _image_size(png) == (220, 260)


@pytest.mark.asyncio
async def test_read_docx_media_with_jpeg(tmp_path: Path) -> None:
    """含 JPEG 媒体的 docx read 不应抛错，且能解析出尺寸与人像启发式。"""
    source = tmp_path / "jpeg_resume.docx"
    _docx_with_images(
        source,
        [
            ("rId4", "media/photo.jpg", _jpeg_bytes(300, 400)),  # 竖版近方形 → 人像
            ("rId5", "media/banner.jpg", _jpeg_bytes(800, 120)),  # 横版条幅
        ],
    )
    registry = ToolRegistry(WorkspacePolicy(tmp_path), frozenset())
    call = ToolCall(id="read-jpeg-docx", name="read", arguments={"path": source.name})

    result = await registry.execute(call)

    assert result.success is True, result.content
    media = json.loads(result.content)["media"]
    assert [m["name"] for m in media] == ["photo.jpg", "banner.jpg"]
    assert media[0]["width"] == 300 and media[0]["height"] == 400
    assert media[0]["portrait_likely"] is True
    assert media[1]["portrait_likely"] is False


def test_fill_resume_replaces_photo_and_rewrites_rels(tmp_path: Path) -> None:
    """fill_resume with photo=<user image> swaps the media bytes, rewrites the rels
    Target for that rId (attribute-order independent), and registers the extension
    in [Content_Types].xml so Word doesn't report the file as corrupt."""
    import subprocess
    import sys
    import zipfile as _zipfile

    skill_dir = (
        Path(__file__).resolve().parents[1]
        / "backend" / "skill_toolbox" / "skill_defs" / "resume_pro"
    )
    script = skill_dir / "scripts" / "fill_resume.py"
    tpl_dir = skill_dir / "templates" / "t001"
    assert script.is_file() and (tpl_dir / "template.docx").is_file(), "resume_pro skill files missing"

    workspace = tmp_path
    user_photo = workspace / "me.png"
    user_photo.write_bytes(_png_bytes(200, 260))
    data = {"fields": {"photo": str(user_photo), "name": "张三"}}
    data_path = workspace / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out_path = workspace / "artifacts" / "resume.docx"

    completed = subprocess.run(
        [sys.executable, str(script), str(tpl_dir), str(data_path), str(out_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(workspace),
    )
    assert completed.returncode == 0, completed.stderr

    with _zipfile.ZipFile(out_path) as z:
        out_names = z.namelist()
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        ct = z.read("[Content_Types].xml").decode("utf-8")
        doc_xml = z.read("word/document.xml").decode("utf-8")
    # 旧媒体字节被替换为新图片（保留 rId4 drawing，仅换源）
    assert "word/media/image1.jpeg" not in out_names
    assert "word/media/image1_user.png" in out_names
    assert "rId4" in doc_xml, "photo drawing must stay (image swapped, not removed)"
    # rels 的 rId4 Target 指向新图片
    assert re.search(r'Id="rId4"[^>]*Target="media/image1_user\.png"', rels) or re.search(
        r'Target="media/image1_user\.png"[^>]*Id="rId4"', rels
    ), rels
    # 新扩展名已注册，Word 不会报文件损坏
    assert re.search(r'<Default Extension="png"', ct, re.IGNORECASE)
