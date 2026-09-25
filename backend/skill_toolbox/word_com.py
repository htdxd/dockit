"""Word/WPS 文字的 COM 选择、身份检查及清理；两者都实现测量所用的 Word 对象模型。"""
import os
from pathlib import Path
import re
import sys

# 引擎 → (ProgID, COM 注册应指向的主程序, 显示名)；自动选择时按此顺序，Word 优先。
ENGINES = {
    'word': ('Word.Application', 'winword.exe', 'Microsoft Word'),
    'wps': ('KWPS.Application', 'wps.exe', 'WPS 文字'),
}
ENGINE_ENV = 'DOCKIT_OFFICE_ENGINE'
E_NOTIMPL = -2147467263


def _registered_command(progid: str) -> str:
    import winreg
    # WPS 只在 32 位注册表视图登记 COM 服务器，64 位 Python 默认视图看不到。
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            access = winreg.KEY_READ | view
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + '\\CLSID', 0, access) as key:
                clsid = str(winreg.QueryValueEx(key, None)[0])
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f'CLSID\\{clsid}\\LocalServer32', 0, access) as key:
                return str(winreg.QueryValueEx(key, None)[0])
        except OSError:
            continue
    raise OSError(f'{progid} 缺少 COM 注册')


def select_engine() -> tuple[str, str]:
    """返回 (引擎, COM 注册命令)；DOCKIT_OFFICE_ENGINE=word|wps 强制指定，否则 Word 优先、WPS 兜底。"""
    if sys.platform != 'win32':
        raise RuntimeError('WORD_UNAVAILABLE: 简历测量需要 Windows 上的 Microsoft Word 或 WPS 文字。')
    forced = os.environ.get(ENGINE_ENV, '').strip().lower()
    if forced and forced not in ENGINES:
        raise RuntimeError(f'WORD_UNAVAILABLE: {ENGINE_ENV}={forced} 无效，只能是 word 或 wps。')
    problems = []
    for engine in [forced] if forced else list(ENGINES):
        progid, exe, _ = ENGINES[engine]
        try:
            command = _registered_command(progid)
        except OSError:
            problems.append(f'未找到 {progid} 注册')
            continue
        # 只装 WPS 时 Word.Application 常被接管为 wps.exe；它按 KWPS 注册识别，不冒充 Word。
        if re.search(rf'(?i)(?:^|[\\/"]){re.escape(exe)}(?:["\s]|$)', command):
            return engine, command
        problems.append(f'{progid} 注册的不是 {exe}：{command}')
    raise RuntimeError('WORD_UNAVAILABLE: 未找到可用的 Microsoft Word 或 WPS 文字 COM 注册（'
                       + '；'.join(problems) + '），请安装其中之一。')


def validate_version(version: str):
    if float(version) < 14:
        raise RuntimeError(f'WORD_UNAVAILABLE: Word COM {version} 不支持当前 Office 2010 文本框结构；请使用 Microsoft Word 16.x 系列并重新执行 --check。')


def _line_bottom(last) -> float | None:
    """末字符所在行的行底；-1 行位表示已被挤出版面。"""
    top = float(last.Information(6))
    if top < 0:
        return None
    fmt = last.ParagraphFormat
    if int(fmt.LineSpacingRule) == 4:  # wdLineSpaceExactly
        return top + float(fmt.LineSpacing)
    # 单倍/最小值行距的行高取决于字体度量，COM 不直接给出；临时追加一行读取下一行行位后删除。
    probe = last.Duplicate
    probe.Collapse(0)  # wdCollapseEnd
    probe.InsertAfter('\r　')
    try:
        following = float(probe.Characters(probe.Characters.Count).Information(6))
    finally:
        probe.Delete()
    if following <= top:
        return None
    return following - float(fmt.SpaceAfter) - float(fmt.SpaceBefore)


def text_overflows(shape) -> bool:
    """文本框是否装不下全部文字。WPS 未实现 Overflowing，改用末行实际行底与框内底边比较。"""
    frame = shape.TextFrame
    try:
        return bool(frame.Overflowing)
    except Exception as exc:
        info = getattr(exc, 'excepinfo', None) or ()
        if E_NOTIMPL not in (getattr(exc, 'hresult', None), *info[5:6]):
            raise
    text = frame.TextRange
    visible = len(text.Text.rstrip('\r\n\x07 '))
    if not visible:
        return False
    bottom = _line_bottom(text.Characters(visible))
    if bottom is None:
        return True
    # WPS 行位按整点返回，留半点舍入容差。
    return bottom > float(shape.Top) + float(shape.Height) - float(frame.MarginBottom) + 0.5


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
    engine, _ = select_engine()
    progid, exe, name = ENGINES[engine]
    pythoncom.CoInitialize()
    word = None
    try:
        word = client.DispatchEx(progid)
        # Word 的动态 IDispatch 偶尔把 Quit 报成属性；按明确的 COM 方法调用。
        if isinstance(word, client.dynamic.CDispatch):
            word._FlagAsMethod('Quit')
        # WPS 对外自称 Microsoft Word 12.0，版本号只对真正的 Word 有意义。
        if engine == 'word':
            validate_version(str(word.Version))
        if not (Path(str(word.Path)) / exe).is_file():
            raise RuntimeError(f'WORD_UNAVAILABLE: COM 程序目录中没有 {exe}，请修复 {name} 的安装或注册。')
        word.Visible = False
        word.DisplayAlerts = 0
        return word
    except BaseException:
        close_word(word)
        raise
