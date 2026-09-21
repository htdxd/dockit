"""材料资源的路径、稳定标识与安全复制；不包含解析策略或缓存。"""
import hashlib
import re
import shutil
from pathlib import Path

from skill_toolbox.material_models import Asset, SourceFormat
from skill_toolbox.tools.workspace import sha256_file

_REF_SAFE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

COMPAT_MATERIALS_DIR = "work/materials"

class MaterialError(RuntimeError):
    """材料处理错误。code 为稳定错误码（实施计划 §9.1）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def safe_stem(name: str) -> str:
    """安全化 stem：只把路径分隔符等不安全字符换成下划线，保留可读字符。

    Path(name).stem 在 Windows 上对 "a/b" 只取 "b"（/ 视作分隔符）；这里先
    把分隔符替换掉再取 stem，保证 "a/b" → "a_b"（不丢失目录语义）。
    """
    stem = re.sub(r'[/\\:*?"<>|\x00\s]', "_", name)
    stem = Path(stem).stem
    stem = stem.strip("._")
    return stem or "material"


def material_id_for(source: Path) -> str:
    """material_id = 源文件 SHA-256 全量（manifest 用）+ 安全化扩展名。

    目录名使用前 16 位；发生前缀冲突时扩展长度（阶段 7 验证，当前 16 位
    足够区分测试材料）。
    """
    return f"{sha256_file(source)}{source.suffix.lower()}"


def _material_scope(material_id: str) -> str:
    """短目录/对象 scope 必须包含完整 material_id（含 source suffix）。"""
    return hashlib.sha256(material_id.encode("utf-8")).hexdigest()[:16]


def _block_id(material_id: str, order: int) -> str:
    return f"block-{_material_scope(material_id)}-{order}"


def _asset_id(material_id: str, locator: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{material_id}:{locator}:{content_hash}".encode()
    ).hexdigest()[:12]
    return f"asset-{_material_scope(material_id)}-{digest}"


def _format_for(suffix: str) -> SourceFormat:
    if suffix == ".md" or suffix == ".markdown":
        return "md"
    if suffix == ".txt":
        return "txt"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    return "image"


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }
    return mime.get(ext, "application/octet-stream")


def material_id_dir(workspace: Path, material_id: str) -> Path:
    """材料 IR 根目录（work/materials/<id16>/）；统一命名，避免各处拼接。"""
    return workspace / COMPAT_MATERIALS_DIR / _material_scope(material_id)


def _materialize_asset_into(
    workspace: Path, material_id: str, src: Path, out_name: str | None = None
) -> Asset:
    """把媒体物化到材料 assets/ 并登记 Asset（内容 hash 派生 id）。

    asset_id 由材料 ID + 来源 locator + 内容 hash 派生，不能只用原始文件名
    （实施计划 §7）。返回登记后的 Asset。
    """
    material_root = material_id_dir(workspace, material_id)
    assets_dir = material_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    out = assets_dir / (out_name or src.name)
    if not out.exists():
        shutil.copy2(src, out)
    content_hash = sha256_file(out)
    asset_id = _asset_id(material_id, out.name, content_hash)
    return Asset(
        id=asset_id,
        path=_relative_to(workspace, out),
        mime_type=_mime_for(out),
        sha256=content_hash,
        **image_dimensions(out),
    )


def _relative_to(workspace: Path, path: Path) -> str:
    return str(path.resolve().relative_to(workspace.resolve()))


def image_dimensions(path: Path) -> dict[str, int | None]:
    from skill_toolbox.tools.assets import image_size

    width, height = image_size(path) or (None, None)
    return {'width': width, 'height': height}
