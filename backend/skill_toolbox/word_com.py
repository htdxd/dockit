"""Microsoft Word 身份检查及 COM 清理；兼容接口不等于 Word。"""
from pathlib import Path
import re
import sys


def check_registered_word() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 'Word.Application\\CLSID') as key:
            clsid = str(winreg.QueryValueEx(key, None)[0])
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f'CLSID\\{clsid}\\LocalServer32') as key:
            command = str(winreg.QueryValueEx(key, None)[0])
    except Exception as exc:
        raise RuntimeError('WORD_UNAVAILABLE: 未找到 Microsoft Word COM 注册，请安装 Microsoft Word。') from exc
    if not re.search(r'(?i)(?:^|[\\/\"])winword\.exe(?:[\"\s]|$)', command):
        raise RuntimeError(f'WORD_UNAVAILABLE: Word.Application 注册的不是 Microsoft Word：{command}。WPS 兼容接口不能用于当前模板测量。')
    return command


def validate_version(version: str):
    if float(version) < 14:
        raise RuntimeError(f'WORD_UNAVAILABLE: Word COM {version} 不支持当前 Office 2010 文本框结构；请使用 Microsoft Word 16.x 系列并重新执行 --check。')


def _cleanup(action):
    primary = sys.exc_info()[1]
    try:
        action()
    except Exception as exc:
        if primary is None:
            raise
        primary.add_note(f'COM 清理也失败：{type(exc).__name__}: {exc}')


def close_document(doc):
    if doc is not None:
        _cleanup(lambda: doc.Close(False))


def close_word(word, doc=None):
    import pythoncom
    try:
        try:
            close_document(doc)
        finally:
            if word is not None:
                _cleanup(lambda: word.Quit())
    finally:
        pythoncom.CoUninitialize()


def start_word():
    import pythoncom
    from win32com import client
    check_registered_word()
    pythoncom.CoInitialize()
    word = None
    try:
        word = client.DispatchEx('Word.Application')
        validate_version(str(word.Version))
        if not (Path(str(word.Path)) / 'WINWORD.EXE').is_file():
            raise RuntimeError('WORD_UNAVAILABLE: COM 程序目录中没有 WINWORD.EXE，请修复 Microsoft Word 的安装或注册。')
        word.Visible = False
        word.DisplayAlerts = 0
        return word
    except BaseException:
        close_word(word)
        raise
