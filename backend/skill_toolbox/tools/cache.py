"""通用 hash cache：原子提交、COMPLETE 标记、per-key 锁（实施计划 §6.2/§6.3）。

布局（MinerU 持久缓存的通用形式，本模块不绑定 MinerU）::

    <cache_root>/<key>/
      data/                        # 提交后的产物（stage 写入 → rename 提交）
        result.docx
        result.json
      metadata.json                # 调用方写入的版本/参数元数据
      COMPLETE                     # 原子提交标记（存在即视为完整命中候选）

锁边界：``lock(key)`` 是 per-key 跨进程文件锁；``lookup``/``stage``/``commit``
/``cleanup_tmp`` 都必须在 ``with cache.lock(key):`` 内调用（单锁序列保证并发
同 key 只转换一次）。

规则：
- 完整命中要求 COMPLETE + metadata + 调用方 validator（产物存在且可读）都通过；
- 提交先写 metadata → rename data 目录 → 写 COMPLETE（最后一步才暴露命中）；
- 失败只清理临时目录，不生成 COMPLETE；损坏/版本不兼容按 miss 处理，
  不覆盖其它有效 cache key。
- Token/密钥只允许在内存与受控子进程环境传递，永不写入缓存与日志。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator


@contextlib.contextmanager
def _per_key_lock(root: Path, key: str, stale_seconds: float = 300.0) -> Iterator[None]:
    """跨进程 per-key 文件锁（create-if-absent + heartbeat + 过期保护）。"""
    entry = root / key
    entry.mkdir(parents=True, exist_ok=True)
    lock_path = entry / ".lock"
    owner = f"{os.getpid()}-{uuid.uuid4().hex}"
    heartbeat_stop = threading.Event()
    heartbeat: threading.Thread | None = None
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, owner.encode("ascii"))
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > stale_seconds:
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                time.sleep(0.05)

        # MinerU 单次转换可能远超 stale_seconds。持锁期间持续刷新 mtime，
        # 避免其它进程把仍在工作的锁误判为僵尸锁并重复调用 API。
        interval = max(0.05, min(30.0, stale_seconds / 3.0))

        def refresh() -> None:
            while not heartbeat_stop.wait(interval):
                try:
                    if lock_path.read_text(encoding="ascii") != owner:
                        return
                    os.utime(lock_path, None)
                except OSError:
                    return

        heartbeat = threading.Thread(
            target=refresh,
            name=f"hash-cache-lock-{key[:8]}",
            daemon=True,
        )
        heartbeat.start()
        yield
    finally:
        heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1.0)
        try:
            if lock_path.read_text(encoding="ascii") == owner:
                lock_path.unlink()
        except OSError:
            pass


def _rand_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _atomic_replace_dir(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


class HashCache:
    """按 key 分目录的持久缓存。根目录默认 ``<app-data>/skill-toolbox-cache``。

    key 由调用方计算（如 pdf_sha256 + 版本 + 参数指纹）；本模块只负责
    目录布局、锁、原子提交与完整性标记。
    """

    def __init__(self, root: Path, *, lock_stale_seconds: float = 300.0) -> None:
        self.root = root.resolve()
        self.lock_stale_seconds = lock_stale_seconds

    def lock(self, key: str) -> contextlib.AbstractContextManager[None]:
        return _per_key_lock(self.root, key, self.lock_stale_seconds)

    def key_dir(self, key: str) -> Path:
        return self.root / key

    def data_dir(self, key: str) -> Path:
        return self.key_dir(key) / "data"

    def lookup(
        self,
        key: str,
        validator: Callable[[Path, dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        """完整命中返回 metadata；未命中/损坏/不完整返回 None（不抛错）。

        必须在 ``with self.lock(key):`` 内调用。命中要求 COMPLETE 存在、
        metadata 合法且 validator 校验通过（产物文件存在、DOCX 可读等）。
        """
        entry = self.key_dir(key)
        if not (entry / "COMPLETE").is_file():
            return None
        meta_path = entry / "metadata.json"
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(metadata, dict):
            return None
        try:
            validator(self.data_dir(key), metadata)
        except (OSError, ValueError):
            return None
        return metadata

    def stage(self, key: str) -> tuple[Path, Path]:
        """创建临时目录（锁内）。返回 (tmp_dir, metadata_path)。

        调用方写入产物后调 commit()；任何失败只清理 tmp_dir，不生成 COMPLETE。
        """
        entry = self.key_dir(key)
        entry.mkdir(parents=True, exist_ok=True)
        tmp = entry / _rand_name(".tmp")
        tmp.mkdir()
        return tmp, tmp / "metadata.json"

    def commit(self, key: str, tmp: Path, metadata: dict[str, Any]) -> None:
        """原子提交（锁内）：metadata → rename data 目录 → 写 COMPLETE。

        metadata 是纯数据 JSON（禁止含 Token/密钥/Authorization 头），写入
        key 目录根（lookup 读取位置）；data/ 目录承载调用方产物。
        """
        entry = self.key_dir(key)
        entry.mkdir(parents=True, exist_ok=True)
        complete = entry / "COMPLETE"
        # 重建损坏缓存时先撤销旧完成标记。后续任一步崩溃都只能形成 miss，
        # 不会让旧 COMPLETE 暴露新旧 data/metadata 的混合状态。
        with contextlib.suppress(OSError):
            complete.unlink()

        final = self.data_dir(key)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        _atomic_replace_dir(tmp, final)
        meta_path = entry / "metadata.json"
        meta_tmp = entry / _rand_name(".tmp-metadata")
        meta_tmp.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        meta_tmp.replace(meta_path)
        complete_tmp = entry / _rand_name(".tmp-complete")
        complete_tmp.write_text("ok\n", encoding="utf-8")
        complete_tmp.replace(complete)

    def cleanup_tmp(self, key: str) -> None:
        """清理未提交的临时目录（失败/取消后调用，不留半成品）。锁内。"""
        entry = self.key_dir(key)
        if not entry.is_dir():
            return
        for child in entry.iterdir():
            if child.name.startswith(".tmp-"):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        child.unlink()

    def discard(self, key: str) -> None:
        """删除整个缓存项（供测试/显式清理）。"""
        shutil.rmtree(self.key_dir(key), ignore_errors=True)
