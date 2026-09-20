"""工具适配保留契约默认值、局部更新语义和运行状态。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from skill_toolbox.contracts.common import OperationResult
from skill_toolbox.llm_tools.dispatcher import DomainServices, dispatch_with_media


def test_v2_dispatch_preserves_partial_entry_and_mode_precedence():
    service = Mock()
    service.generate.return_value = service.repair.return_value = OperationResult(ok=True)
    services = DomainServices(resume_v2=service)
    payload = {"template_id": "t109", "content": {
        "template_id": "t109", "layout_mode": "local", "sections": [],
    }}
    dispatch_with_media("resume_generate_v2", payload, services)
    assert service.generate.call_args.args[0].layout_mode == "local"
    dispatch_with_media("resume_generate_v2", {**payload, "layout_mode": "reflow"}, services)
    assert service.generate.call_args.args[0].layout_mode == "reflow"

    dispatch_with_media("resume_repair_v2", {
        "artifact_id": "a", "base_revision": 1, "request_id": "r",
        "changes": [{"op": "update_entry", "entry": {"bullets": []}}],
    }, services)
    entry = service.repair.call_args.args[0].changes[0].entry
    assert entry.model_fields_set == {"bullets"}


def test_accept_keeps_visual_notes_separate():
    service = Mock()
    service.accept.return_value = OperationResult(ok=True)
    dispatch_with_media("resume_accept", {
        "artifact_id": "a", "candidate_revision": 2, "expected_accepted_revision": 1,
        "visual_notes": "所有页面已检查",
    }, DomainServices(resume_v2=service))
    assert service.accept.call_args.kwargs == {"visual_notes": "所有页面已检查"}
    assert service.accept.call_args.args[0].request_id == ""


def test_nested_contract_validation_returns_tool_error():
    service = Mock()
    text, images, meta = dispatch_with_media("resume_generate_v2", {
        "template_id": "t001", "content": {"template_id":"t001", "sections": [{"title": "缺少 key"}]},
    }, DomainServices(resume_v2=service))
    assert json.loads(text)["code"] == "TOOL_ARGUMENTS_INVALID"
    service.generate.assert_not_called()
    assert images == [] and not meta.get("ok", False)


def test_legacy_numeric_table_cells_and_resume_fields_still_become_text():
    resume = Mock()
    resume.generate.return_value = resume.repair.return_value = OperationResult(ok=True)
    services = DomainServices(resume=resume)
    dispatch_with_media("resume_generate", {
        "template_id": "t001", "fields": {"age": 24, "gpa": 3.5},
    }, services)
    assert resume.generate.call_args.args[0].fields == {"age": "24", "gpa": "3.5"}
    dispatch_with_media("resume_repair", {
        "artifact_id": "r", "changes": [{"action": "replace_text", "text": 2026}],
    }, services)
    assert resume.repair.call_args.args[0].changes[0].text == "2026"


@pytest.mark.parametrize("ok,status,expected", [
    (True, "created", True), (True, "failed", False), (False, "failed", False),
])
def test_media_success_matches_runtime_semantics(ok, status, expected):
    service = Mock()
    service.prepare.return_value = OperationResult(ok=ok, status=status)
    _, _, meta = dispatch_with_media("resume_prepare", {"template_id": "t001"}, DomainServices(resume=service))
    assert meta["ok"] is expected


def test_cancel_includes_v2_and_deduplicates_shared_runner():
    runner = Mock()
    service = SimpleNamespace(runner=runner)
    services = DomainServices(resume=service, resume_v2=service)
    services.cancel_all()
    runner.terminate_all.assert_called_once_with()
    services.cancel_all(permanent=True)
    runner.cancel.assert_called_once_with()
