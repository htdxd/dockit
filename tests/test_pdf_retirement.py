"""Retired creation routes, shared parsing and existing artifact compatibility."""

from pathlib import Path

import pytest

from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest
from skill_toolbox.sidecar import SidecarService
from skill_toolbox.skills import load_skill


@pytest.mark.parametrize("skill_id", ["pdf_docx_routing", "pdf_to_docx"])
async def test_retired_runtime_fails_before_work_or_model(tmp_path, skill_id):
    output = tmp_path / "not-created"
    events = []
    result = await AgentRuntime(ScriptedProvider([]), events.append).run(
        TaskRequest(skill_id, "convert", output)
    )
    assert result.status == "failed"
    assert "FEATURE_RETIRED" in result.error
    assert result.artifacts == []
    assert not output.exists()
    assert events == [{"type": "task_failed", "error": result.error}]
    with pytest.raises(ValueError, match="FEATURE_RETIRED"):
        load_skill(skill_id)


@pytest.mark.parametrize("payload", [
    {"skill_id": "pdf_docx_routing"},
    {"skill_id": "docx_pro", "task_type": "pdf_to_docx"},
])
async def test_retired_sidecar_rejects_before_provider_validation(payload):
    events = []
    service = SidecarService(emit=events.append)
    await service._start_task("retired", payload)
    assert len(events) == 1
    assert events[0]["event"]["type"] == "task_failed"
    assert "FEATURE_RETIRED" in events[0]["event"]["error"]
    assert not service.tasks


def test_ui_retirement_preserves_history_route_and_pdf_uploads():
    ui = Path("src/ui.ts").read_text(encoding="utf-8")
    tasks = Path("src/taskController.ts").read_text(encoding="utf-8")
    artifacts = Path("src/artifacts.ts").read_text(encoding="utf-8")
    assert 'data-tool="pdf"' not in ui
    assert 'id="page-pdf"' not in ui
    assert 'data-start="pdf"' not in ui
    assert 'pdf: "pdf_docx_routing"' not in tasks
    assert 'buckets.docx.push(...(bySkill.pdf ?? []))' in artifacts
    assert 'name.endsWith(".pdf")) buckets.docx.push(f)' in artifacts
    assert 'if (tool === "pdf") { tool = "resume"; sub = "art"; }' in ui
    assert "历史 PDF 转 DOCX（功能已下线）" in artifacts
    assert "pdf / docx / md / txt" in ui


def test_internal_adapter_is_package_local_without_skill_dependency():
    import skill_toolbox.materials as materials

    parser = Path(materials.__file__).parent / "parsers" / "mineru_pdf.py"
    assert parser.is_file()
    assert 'skill_defs' not in parser.read_text(encoding="utf-8")
    assert not (parser.parent.parent / "skill_defs" / "pdf_docx_routing" / "manifest.json").exists()
