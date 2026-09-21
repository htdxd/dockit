"""简历版本文件与工作区幂等索引；不依赖渲染或工具输出。"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.tools.workspace import atomic_write_json


class ResumeStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def artifact_dir(self, artifact_id: str) -> Path:
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id:
            raise ToolError("ARTIFACT_UNKNOWN", f"artifact_id 非法: {artifact_id!r}")
        return self.workspace / "work" / "resume" / artifact_id

    def revision_dir(self, artifact_id: str, revision: int) -> Path:
        return self.artifact_dir(artifact_id) / "revisions" / str(int(revision))

    def index(self, artifact_id: str) -> dict[str, Any]:
        path = self.artifact_dir(artifact_id) / "index.json"
        if not path.is_file():
            raise ToolError(
                "ARTIFACT_UNKNOWN",
                f"{artifact_id} 不存在（先 resume_generate）。",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def save_index(self, artifact_id: str, index: dict[str, Any]) -> None:
        path = self.artifact_dir(artifact_id) / "index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, index)

    def save_revision(self, artifact_id: str, revision: int, record: dict) -> None:
        atomic_write_json(self.revision_dir(artifact_id, revision) / 'revision.json', record)

    def reserve_revision(self, artifact_id: str, revision: int | None = None):
        """调用方持有 artifact 锁；已落盘的版本号不可复用。"""
        root = self.artifact_dir(artifact_id)
        root.mkdir(parents=True, exist_ok=True)
        index = self.index(artifact_id) if (root / 'index.json').is_file() else {'current': 0, 'accepted': 0}
        number = int(revision or index['current'] + 1)
        folder = self.revision_dir(artifact_id, number)
        if (folder / 'revision.json').is_file():
            raise ToolError('REVISION_EXISTS', f'{artifact_id} 的 revision {number} 已存在；版本不可覆盖，请重试。', retryable=True)
        folder.mkdir(parents=True, exist_ok=True)
        return index, number, folder

    def publish_revision(self, artifact_id: str, index: dict, revision: int):
        index['current'] = revision
        self.save_index(artifact_id, index)

    def accept_revision(self, artifact_id: str, index: dict, record: dict, *, notes: str, visual_status: str):
        revision = record['revision']
        record['accepted'] = True
        visual = record.setdefault('visual', {})
        visual['notes'] = notes
        visual['status'] = 'recorded' if notes else visual.get('status', 'not_run')
        self.save_revision(artifact_id, revision, record)
        index['accepted'] = revision
        index.setdefault('accepted_log', []).append({'revision': revision, 'visual': visual_status, 'notes': notes})
        self.save_index(artifact_id, index)

    def record_delivery(self, artifact_id: str, revision: int, pages: list[int]):
        record = self.load_revision(artifact_id, revision)
        visual = record.setdefault('visual', {'status': 'pending', 'delivered_pages': []})
        merged = sorted(set(visual.get('delivered_pages', [])) | set(pages))
        visual['delivered_pages'] = merged
        if merged:
            visual['status'] = 'delivered' if len(merged) >= int(record.get('page_count') or 0) else 'partial'
        self.save_revision(artifact_id, revision, record)
        return record

    def load_revision(self, artifact_id: str, revision: int) -> dict[str, Any]:
        path = self.revision_dir(artifact_id, revision) / "revision.json"
        if not path.is_file():
            raise ToolError(
                "REVISION_UNKNOWN",
                f"{artifact_id} 无 revision {revision}"
                f"（当前 {self.index(artifact_id).get('current')}）。",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- 工作区级 request_id 索引（幂等跨 artifact 生效，计划 §6.3） ----

    def artifact_lock(self, artifact_id: str, *, timeout: float = 180.0):
        return self._lock(artifact_id, timeout=timeout, stale_after=300.0, interval=0.05)

    @property
    def requests_path(self) -> Path:
        return self.workspace / "work" / "resume" / "_requests.json"

    def request_lock(self, *, timeout: float = 60.0):
        return self._lock("_requests", timeout=timeout, stale_after=120.0, interval=0.02)

    @contextlib.contextmanager
    def _lock(self, name: str, *, timeout: float, stale_after: float, interval: float):
        """artifact 级**跨进程**互斥（CAS 保护）。

        文件原子替换只保证「读到的 json 不是半截」，不保证
        「检查版本 → 写回」之间没有其它写者插入。这里用 O_EXCL 独占文件把
        同一 artifact 的 read-modify-write 串行化；陈旧锁按 mtime 过期回收。
        """
        lock_dir = self.workspace / "work" / "resume" / "_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{name}.lock"
        token = f"{os.getpid()}-{uuid.uuid4().hex}"
        deadline = time.time() + timeout
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, token.encode("ascii"))
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > stale_after:
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise ToolError(
                        "VERSION_BUSY",
                        "同一产物上有其它写操作正在进行（等待超时）。",
                        retryable=True,
                        suggestion="稍后重试同一 request_id（幂等）即可。",
                    )
                time.sleep(interval)
        try:
            yield
        finally:
            try:
                if lock_path.read_text(encoding="ascii") == token:
                    lock_path.unlink()
            except OSError:
                pass

    def request_index(self) -> dict[str, Any]:
        path = self.requests_path
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def save_request_index(self, index: dict[str, Any]) -> None:
        path = self.requests_path
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, index)

    def lookup_request(
        self, request_id: str, payload_sig: str
    ) -> dict[str, Any] | None:
        """完整请求幂等：同 id 同载荷 → 既有记录；同 id 不同载荷 → 冲突。"""
        if not request_id:
            return None
        prev = self.request_index().get(request_id)
        if prev is None:
            return None
        if prev.get("payload") != payload_sig:
            raise ToolError(
                "REQUEST_CONFLICT",
                f"request_id={request_id} 已用于不同载荷"
                f"（artifact={prev.get('artifact_id')} revision={prev.get('revision')}）。",
                suggestion="换一个新的 request_id；幂等键必须与载荷一一对应。",
            )
        return prev

    def record_request(
        self, request_id: str, payload_sig: str, entry: dict[str, Any]
    ) -> None:
        """登记幂等记录（读-改-写在**请求索引锁**内完成，跨 artifact 安全）。"""
        if not request_id:
            return
        with self.request_lock():
            index = self.request_index()
            index[request_id] = {"payload": payload_sig, **entry}
            self.save_request_index(index)
