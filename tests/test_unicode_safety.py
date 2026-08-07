from pathlib import Path

import pytest
from skill_toolbox.models import ToolCall
from skill_toolbox.policy import WorkspacePolicy
from skill_toolbox.tools import ToolRegistry

import json


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
