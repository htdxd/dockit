from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import Any


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a deterministic UTF-8 environment for Python child scripts."""
    return {**os.environ, **(extra or {}), "PYTHONIOENCODING": "utf-8"}


def process_group_kwargs() -> dict[str, Any]:
    """Popen flags that let cancellation terminate the child process tree."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
    """Terminate a process and its descendants, then reap the direct child."""
    if proc.poll() is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=timeout)
        return

    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                with contextlib.suppress(OSError):
                    proc.kill()
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            with contextlib.suppress(OSError):
                proc.kill()

    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc.wait(timeout=timeout)
