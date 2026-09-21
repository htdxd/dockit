"""受控 subprocess：超时、进程树回收（实施计划 §3.2）。

比 ``subprocess_utils`` 高一层：供领域 Service 同步调用（经 asyncio.to_thread
执行时可被 Runtime 取消），带活动进程注册与 terminate_all。
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from skill_toolbox.subprocess_utils import (
    child_env,
    process_group_kwargs,
    terminate_process_tree,
)


class ProcessRunner:
    """材料解析与文档构建共用的受控 subprocess runner。

    cancel() 后新任务立即拒绝，活动进程被进程树终止；线程内超时由调用方
    捕获 subprocess.TimeoutExpired。
    """

    def __init__(self, extra_env: dict[str, str] | None = None) -> None:
        self.extra_env = extra_env or {}
        self._active: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        self.terminate_all()

    def terminate_all(self) -> None:
        """终止当前活动进程，但允许同一 Service 后续重试。"""
        with self._lock:
            active = list(self._active)
        for proc in active:
            terminate_process_tree(proc)
        with self._lock:
            self._active.clear()

    def run(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if self._cancelled.is_set():
            raise RuntimeError("process runner was cancelled")
        env = child_env({**self.extra_env, **(extra_env or {})})
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(cwd) if cwd is not None else None,
            "env": env,
            "close_fds": True,
        }
        kwargs.update(process_group_kwargs())
        proc = subprocess.Popen(command, **kwargs)
        with self._lock:
            self._active.append(proc)
        try:
            if self._cancelled.is_set():
                terminate_process_tree(proc)
                raise RuntimeError("process runner was cancelled")
            if timeout is None:
                out, err = proc.communicate()
            else:
                try:
                    out, err = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    terminate_process_tree(proc)
                    raise
            return subprocess.CompletedProcess(proc.args, proc.returncode or 0, out, err)
        finally:
            with self._lock:
                if proc in self._active:
                    self._active.remove(proc)
