import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from skill_toolbox.word_com import check_registered_word, validate_version, close_word


def test_wps_registration_rejected_before_start(monkeypatch):
    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            HKEY_CLASSES_ROOT=0,
            OpenKey=lambda _, path: Key(path),
            QueryValueEx=lambda key, _: (
                "{word}"
                if key.path.endswith("CLSID")
                else '"C:\\Kingsoft\\wps.exe" /Automation',
                0,
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="不是 Microsoft Word"):
        check_registered_word()


def test_old_word_version_rejected():
    with pytest.raises(RuntimeError, match="WORD_UNAVAILABLE"):
        validate_version("12.0")
    validate_version("16.0")


def test_cleanup_preserves_original_failure_and_uninitializes(monkeypatch):
    com = SimpleNamespace(CoUninitialize=Mock())
    monkeypatch.setitem(sys.modules, "pythoncom", com)
    word = SimpleNamespace(
        Quit=Mock(side_effect=AttributeError("Word.Application.Quit"))
    )
    doc = SimpleNamespace(Close=Mock(side_effect=RuntimeError("close failed")))
    with pytest.raises(ValueError, match="original shape missing") as error:
        try:
            raise ValueError("original shape missing")
        finally:
            close_word(word, doc)
    assert len(error.value.__notes__) == 2
    com.CoUninitialize.assert_called_once()
