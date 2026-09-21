"""本地图片尺寸读取，不依赖 Agent 通用文件工具。"""

from pathlib import Path


def image_size(path: Path) -> tuple[int, int] | None:
    """读取图片像素尺寸（纯标准库：JPEG SOF 段 / PNG IHDR），失败返回 None。"""
    import struct  # PNG/JPEG 两分支都要用，不能只在 PNG 分支内 import（JPEG 会 UnboundLocalError）

    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                # marker 前缀的 0xFF 可重复出现（填充字节），跳过；marker 落在 i 处
                while i < len(data) and data[i] == 0xFF:
                    i += 1
                if i + 1 >= len(data):
                    break
                marker = data[i]
                if marker in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }:
                    # SOF 段：marker(1) + 段长(2) + precision(1) + height(2) + width(2)
                    h, w = struct.unpack(">HH", data[i + 4 : i + 8])
                    return (w, h) if w and h else None
                # 其它带长度段：跳过 2 字节段长 + 段长本身（i+1 指向段长首字节）
                seg_len = struct.unpack(">H", data[i + 1 : i + 3])[0]
                i += 1 + seg_len
        return None
    except (struct.error, IndexError):
        return None
