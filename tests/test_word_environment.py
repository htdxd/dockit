import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from skill_toolbox import word_com
from skill_toolbox.word_com import close_word, select_engine, text_overflows, validate_version

WORD = '"C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE" /Automation'
WPS = '"D:\\WPS\\office6\\wps.exe" /prometheus /wps /Automation'


def registry(monkeypatch, commands):
    """按 ProgID 伪造 COM 注册；None 表示未注册。"""
    def fake(progid):
        if commands.get(progid) is None:
            raise OSError(progid)
        return commands[progid]
    monkeypatch.setattr(word_com, '_registered_command', fake)
    monkeypatch.setattr(word_com.sys, 'platform', 'win32')


@pytest.mark.parametrize('commands, forced, expected', [
    ({'Word.Application': WORD, 'KWPS.Application': WPS}, '', 'word'),
    ({'Word.Application': None, 'KWPS.Application': WPS}, '', 'wps'),
    # 只装 WPS 时 Word.Application 被接管为 wps.exe，不能当作 Word。
    ({'Word.Application': WPS, 'KWPS.Application': WPS}, '', 'wps'),
    ({'Word.Application': WORD, 'KWPS.Application': WPS}, 'wps', 'wps'),
    ({'Word.Application': WORD, 'KWPS.Application': WPS}, 'WORD', 'word'),
])
def test_engine_selection_prefers_word_and_falls_back_to_wps(monkeypatch, commands, forced, expected):
    registry(monkeypatch, commands)
    monkeypatch.setenv(word_com.ENGINE_ENV, forced)
    assert select_engine()[0] == expected


@pytest.mark.parametrize('commands, forced', [
    ({'Word.Application': WPS, 'KWPS.Application': None}, ''),
    ({'Word.Application': None, 'KWPS.Application': None}, ''),
    ({'Word.Application': None, 'KWPS.Application': WPS}, 'word'),
    ({'Word.Application': WORD, 'KWPS.Application': WPS}, 'libreoffice'),
])
def test_engine_selection_fails_with_word_unavailable(monkeypatch, commands, forced):
    registry(monkeypatch, commands)
    monkeypatch.setenv(word_com.ENGINE_ENV, forced)
    with pytest.raises(RuntimeError, match='WORD_UNAVAILABLE'):
        select_engine()


def test_registry_reads_wps_from_32bit_view(monkeypatch):
    """KWPS 的 CLSID/LocalServer32 只存在于 WOW6432Node；64 位视图查不到时必须回退。"""
    wow64_64, wow64_32 = 0x100, 0x200
    entries = {
        ('KWPS.Application\\CLSID', wow64_32): '{wps}',
        ('CLSID\\{wps}\\LocalServer32', wow64_32): WPS,
    }

    class Key(SimpleNamespace):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_key(_root, path, _reserved, access):
        view = access & (wow64_64 | wow64_32)
        if (path, view) not in entries:
            raise OSError(path)
        return Key(value=entries[path, view])

    monkeypatch.setitem(sys.modules, 'winreg', SimpleNamespace(
        HKEY_CLASSES_ROOT=0, KEY_READ=1, KEY_WOW64_64KEY=wow64_64, KEY_WOW64_32KEY=wow64_32,
        OpenKey=open_key, QueryValueEx=lambda key, _: (key.value, 1)))
    assert word_com._registered_command('KWPS.Application') == WPS
    with pytest.raises(OSError):
        word_com._registered_command('Word.Application')


def test_old_word_version_rejected():
    with pytest.raises(RuntimeError, match="WORD_UNAVAILABLE"):
        validate_version("12.0")
    validate_version("16.0")


class NotImplementedCom(Exception):
    """模拟 pywintypes.com_error：WPS 对 Overflowing 返回 E_NOTIMPL。"""
    def __init__(self):
        super().__init__(-2147352567, '发生意外。', (0, None, None, None, 0, word_com.E_NOTIMPL), None)
        self.hresult, self.excepinfo = -2147352567, (0, None, None, None, 0, word_com.E_NOTIMPL)


class Frame(SimpleNamespace):
    """Overflowing 读取时抛出给定异常；专用子类，避免改动 SimpleNamespace 本身。"""
    @property
    def Overflowing(self):
        raise self.error


def wps_shape(last_top, height, text='第一行\r第二行\r', rule=4, following=None):
    """末字符 Mock：rule=4 为固定行距 18pt；其他行距时插入探测行，following 为其行位。"""
    probe = Mock()
    probe.Characters.return_value = SimpleNamespace(Information=lambda kind: following)
    probe.Characters.Count = 2
    last = SimpleNamespace(Information=lambda kind: last_top, Duplicate=probe,
                           ParagraphFormat=SimpleNamespace(LineSpacingRule=rule, LineSpacing=18.0,
                                                           SpaceBefore=0.0, SpaceAfter=0.0))
    frame = Frame(MarginBottom=3.6, error=NotImplementedCom(),
                  TextRange=SimpleNamespace(Text=text, Characters=lambda index: last))
    return SimpleNamespace(TextFrame=frame, Top=55.0, Height=height), probe


@pytest.mark.parametrize('last_top, height, expected', [
    (77.0, 44.0, False),   # 行底 95 ≤ 框内底 95.4
    (77.0, 43.0, True),    # 行底 95 > 框内底 94.4（含半点容差）
    (-1.0, 200.0, True),   # 末字符被挤出版面，行位无效
])
def test_wps_overflow_uses_last_character_line_bottom(last_top, height, expected):
    shape, probe = wps_shape(last_top, height)
    assert text_overflows(shape) is expected
    probe.InsertAfter.assert_not_called()


@pytest.mark.parametrize('following, expected', [(94.0, False), (96.0, True), (77.0, True)])
def test_wps_overflow_probes_next_line_for_font_based_spacing(following, expected):
    """单倍行距的行高由字体度量决定：临时追加一行读取行位，读完必须删除。"""
    shape, probe = wps_shape(77.0, 44.0, rule=0, following=following)
    assert text_overflows(shape) is expected
    probe.InsertAfter.assert_called_once()
    probe.Delete.assert_called_once()


def test_overflow_prefers_native_property_and_reraises_other_com_errors():
    assert text_overflows(SimpleNamespace(TextFrame=SimpleNamespace(Overflowing=-1))) is True
    with pytest.raises(RuntimeError, match='RPC'):
        text_overflows(SimpleNamespace(TextFrame=Frame(error=RuntimeError('RPC server unavailable'))))
    assert text_overflows(wps_shape(77.0, 10.0, text='\r')[0]) is False


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
