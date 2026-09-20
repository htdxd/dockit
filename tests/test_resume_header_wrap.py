"""求职意向已换行但整框尚未溢出的正常情形。"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from skill_toolbox.resume_layout import t001

TEMPLATE = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates/t001/template.docx"


@pytest.mark.parametrize("wrapped", [False, True])
def test_header_width_changes_only_when_word_reports_target_role_wrapped(tmp_path, monkeypatch, wrapped):
    class Paragraphs:
        Count = 1

        def __call__(self, index):
            return SimpleNamespace(Range=SimpleNamespace(
                Text="求职意向：Python 后端/AI 应用开发\r",
                ComputeStatistics=lambda _: 2 if wrapped else 1))

    frame = SimpleNamespace(Overflowing=False, TextRange=SimpleNamespace(Paragraphs=Paragraphs()))
    left = SimpleNamespace(Left=57.6, Width=176.75, Top=139.75, Height=99.45, TextFrame=frame)
    right = SimpleNamespace(Left=255.85, Width=179.4, Top=139.75, Height=99.45, TextFrame=frame)
    shapes = {"ResumeHeader22": left, "ResumeHeader62": right}
    doc = SimpleNamespace(Shapes=lambda name: shapes[name], Close=lambda _: None)
    word = SimpleNamespace(Documents=SimpleNamespace(Open=lambda *args: doc), Quit=lambda: None)
    monkeypatch.setattr("skill_toolbox.word_com.start_word", lambda: word)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(
        CoInitialize=lambda: None, CoUninitialize=lambda: None))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(
        client=SimpleNamespace(DispatchEx=lambda _: word)))
    fields = {"target_role": "Python 后端/AI 应用开发"}
    result = t001.fit_header(TEMPLATE, fields, tmp_path)
    assert result["22"]["width_pt"] == pytest.approx(191.05 if wrapped else 176.75)
    assert result["22"]["height_pt"] == 99.45
    assert result["62"]["width_pt"] == 179.4
    assert fields == {"target_role": "Python 后端/AI 应用开发"}
