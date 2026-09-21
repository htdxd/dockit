from pathlib import Path
from PIL import Image

def test_image_size_parses_jpeg_and_png(tmp_path: Path) -> None:
    """JPEG SOF / PNG IHDR 尺寸解析——回归：import struct 曾只写在 PNG 分支内，
    导致任何 JPEG 都抛 UnboundLocalError，read 含照片的旧简历 docx 整体失败。"""
    from skill_toolbox.tools.assets import image_size as _image_size

    jpg = tmp_path / "photo.jpg"
    Image.new('RGB', (300,400)).save(jpg)
    assert _image_size(jpg) == (300, 400)
    png = tmp_path / "photo.png"
    Image.new('RGB', (220,260)).save(png)
    assert _image_size(png) == (220, 260)


# ---------------- ingest 统一 IR 萃取 ----------------


def test_redact_secrets_strips_api_keys_from_error_text() -> None:
    """probe 错误文本的 api_key 脱敏：UI/DB 不得出现 sk-* 明文。"""
    from skill_toolbox.unicode_utils import redact_secrets

    raw = (
        "Error code: 401 - Incorrect API key provided: sk-leaktest-abcdef123456. "
        'body: {"error": {"message": "Invalid api_key: sk-leaktest-abcdef123456"}} '
        "?key=sk-another-zzz999888777"
    )
    out = redact_secrets(raw)
    assert "sk-leaktest-abcdef123456" not in out
    assert "sk-another-zzz999888777" not in out
    assert "REDACTED" in out
    # 普通文本不受影响
    assert redact_secrets("connection refused") == "connection refused"
