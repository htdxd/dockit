"""生产路径与受控执行边界，不依赖 Word。"""
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.resume_layout import pipeline, renderer
from skill_toolbox.resume_layout.t109 import TEMPLATE
from skill_toolbox.tools.process import ProcessRunner
from skill_toolbox.tools.resume_edit import ResumeEditService


def test_measurement_worker_uses_supplied_template_and_excludes_photo(tmp_path):
    templates = tmp_path / "custom" / "templates"
    template = templates / "t109" / "template.docx"
    template.parent.mkdir(parents=True)
    shutil.copyfile(TEMPLATE, template)
    commands = []

    class MeasurementCaptured(Exception):
        pass

    class Runner:
        def run(self, command, **kwargs):
            commands.append((command, kwargs))
            raise MeasurementCaptured

    svc = ResumeEditService(tmp_path / "workspace", templates, runner=Runner())
    scenario = {"sections": [], "header": {"photo_bytes": b"binary image"}}
    with pytest.raises(MeasurementCaptured):
        renderer.build(scenario, {'sections': []}, svc.workspace / 'build-output',
            template=template, profile=svc.profile, run_stage=svc._run_stage)
    command, options = commands[0]
    assert command[1:3] == ["-m", "skill_toolbox.resume_layout.pipeline"]
    assert Path(command[3]) == template
    assert json.loads(Path(command[4]).read_text(encoding="utf-8")) == {"sections": [], 'header': {}}
    assert options["cwd"] == svc.workspace
    assert options["timeout"] == 300


def test_emit_requires_and_reads_supplied_template(tmp_path):
    custom = tmp_path / "custom.docx"
    # 没有隐式回退到包内原件：输入不存在时应直接拒绝。
    with pytest.raises(FileNotFoundError):
        pipeline.emit_scenario({}, None, tmp_path / "out.docx", template=custom)


def test_service_stage_cancel_terminates_active_worker(tmp_path, monkeypatch):
    import skill_toolbox.tools.process as process

    started = threading.Event()
    communicate = process.subprocess.Popen.communicate

    def communicate_started(proc, *args, **kwargs):
        started.set()
        return communicate(proc, *args, **kwargs)

    monkeypatch.setattr(process.subprocess.Popen, "communicate", communicate_started)
    runner = ProcessRunner()
    svc = ResumeEditService(tmp_path, TEMPLATE.parent.parent, runner=runner)
    # 等待事件的隔离进程；只测试 runner 生命周期，不启动 Word。
    command = [sys.executable, "-c", "import threading; threading.Event().wait()"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(svc._run_stage, command, "MEASURE_FAILED")
        assert started.wait(5)
        runner.cancel()
        with pytest.raises(ToolError) as error:
            task.result(timeout=5)
        assert error.value.code == "MEASURE_FAILED"
    assert not runner._active
