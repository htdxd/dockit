"""共享原子写保持并发写入及 Windows 临时占用行为。"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

from skill_toolbox.tools import workspace


def test_concurrent_writes_use_independent_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    barrier = Barrier(2)
    replace = workspace.os.replace
    sources = []
    writer = local()

    def synchronized_replace(source, destination):
        if not getattr(writer, "started", False):
            writer.started = True
            sources.append(source)
            barrier.wait(timeout=5)
        replace(source, destination)

    monkeypatch.setattr(workspace.os, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda n: workspace.atomic_write_json(target, {"n": n}), [1, 2]))
    assert len(set(sources)) == 2
    assert json.loads(target.read_text()) in [{"n": 1}, {"n": 2}]
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_retries_temporary_file_access_denial(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    replace = workspace.os.replace
    attempts = 0

    def temporarily_busy(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("file is in use")
        replace(source, destination)

    monkeypatch.setattr(workspace.os, "replace", temporarily_busy)
    workspace.atomic_write_json(target, {"state": "ready"})
    assert attempts == 2
    assert json.loads(target.read_text()) == {"state": "ready"}
