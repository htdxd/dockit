"""Shared renderer contract checks without starting Word or LibreOffice."""
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from skill_toolbox import docx_render


@pytest.mark.parametrize("word_available", [True, False])
def test_render_pipeline_preserves_cli_output(tmp_path, monkeypatch, capsys, word_available):
    monkeypatch.delenv("DOCKIT_OFFICE_ENGINE", raising=False)
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


@pytest.mark.parametrize("domain", ["resume_pro"])
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


@pytest.mark.parametrize("engine, progid", [("word", "Word.Application"), ("wps", "KWPS.Application")])
def test_word_export_uses_measurement_engine(tmp_path, monkeypatch, engine, progid):
    from skill_toolbox import word_com

    monkeypatch.setattr(word_com, "select_engine", lambda: (engine, "registered"))
    scripts = []

    def capture(command, timeout):
        scripts.append(base64.b64decode(command[-1]).decode("utf-16-le"))
        (tmp_path / "out.pdf").touch()
        return 0, "", ""

    monkeypatch.setattr(docx_render, "_run_capture", capture)
    docx_render._render_with_word(tmp_path / "in.docx", tmp_path / "out.pdf")
    assert f"New-Object -ComObject {progid};" in scripts[0]
    assert "try {if ($null -ne $doc) {$doc.Close($false)}}" in scripts[0]
    assert "finally {if ($null -ne $word) {$word.Quit()}}" in scripts[0]


@pytest.mark.parametrize("engine", ["wps", "word", "invalid"])
def test_forced_engine_failure_never_falls_back(tmp_path, monkeypatch, capsys, engine):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKIT_OFFICE_ENGINE", engine)
    (tmp_path / "input.docx").touch()
    monkeypatch.setattr(sys, "argv", ["render_pages.py", "input.docx", "preview"])

    def failed(*args):
        raise RuntimeError("office export failed")

    def unexpected(*args):
        pytest.fail("显式引擎失败不应切换到 LibreOffice")

    monkeypatch.setattr(docx_render, "_render_with_word", failed)
    monkeypatch.setattr(docx_render, "_render_with_soffice", unexpected)
    with pytest.raises(SystemExit) as exc:
        docx_render.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "office export failed"


def test_fallback_failure_preserves_office_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOCKIT_OFFICE_ENGINE", raising=False)
    (tmp_path / "input.docx").touch()
    monkeypatch.setattr(sys, "argv", ["render_pages.py", "input.docx", "preview"])

    def office(*args):
        raise RuntimeError("WPS COM unavailable")

    def soffice(*args):
        raise OSError("soffice missing")

    monkeypatch.setattr(docx_render, "_render_with_word", office)
    monkeypatch.setattr(docx_render, "_render_with_soffice", soffice)
    with pytest.raises(SystemExit) as exc:
        docx_render.main()
    assert exc.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "WPS COM unavailable" in error and "soffice missing" in error
