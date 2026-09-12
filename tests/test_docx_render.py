"""Shared renderer contract checks without starting Word or LibreOffice."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from skill_toolbox import docx_render


@pytest.mark.parametrize("word_available", [True, False])
def test_render_pipeline_preserves_cli_output(tmp_path, monkeypatch, capsys, word_available):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "简历.docx"
    source.touch()
    monkeypatch.setattr(sys, "argv", ["render_pages.py", source.name, "preview"])
    calls = []

    def word(src, pdf):
        calls.append("word")
        assert src == source
        if not word_available:
            raise RuntimeError("Word unavailable")
        pdf.touch()

    def soffice(src, pdf):
        calls.append("soffice")
        pdf.touch()

    def rasterize(command, timeout):
        assert command == ["pdftoppm", "-png", "-r", "120",
                           str(tmp_path / "preview/简历.pdf"), str(tmp_path / "preview/page")]
        assert timeout == 180
        (tmp_path / "preview/page-1.png").touch()
        return 0, "", ""

    monkeypatch.setattr(docx_render, "_render_with_word", word)
    monkeypatch.setattr(docx_render, "_render_with_soffice", soffice)
    monkeypatch.setattr(docx_render.shutil, "which", lambda name: name)
    monkeypatch.setattr(docx_render, "_run_capture", rasterize)
    docx_render.main()

    assert calls == (["word"] if word_available else ["word", "soffice"])
    assert json.loads(capsys.readouterr().out) == {
        "pdf": str(Path("preview/简历.pdf")),
        "images": [str(Path("preview/page-1.png"))],
        "page_images_created": 1,
    }


@pytest.mark.parametrize("domain", ["resume_pro", "docx_pro"])
def test_legacy_cli_imports_from_any_workspace_and_rejects_escape(tmp_path, domain):
    script = Path(docx_render.__file__).parent / "skill_defs" / domain / "scripts/render_pages.py"
    # Isolated mode proves the wrapper does not need a repository PYTHONPATH.
    usage = subprocess.run([sys.executable, "-I", str(script)], cwd=tmp_path,
                           capture_output=True)
    assert usage.returncode == 2
    assert b"Usage: render_pages.py" in usage.stderr
    escape = subprocess.run([sys.executable, "-I", str(script), "../outside.docx", "preview"],
                            cwd=tmp_path, capture_output=True)
    assert escape.returncode != 0
    assert b"ValueError" in escape.stderr
    assert not (tmp_path / "preview").exists()
