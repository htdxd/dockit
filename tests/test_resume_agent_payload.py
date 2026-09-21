"""模型返回视图减少重复内容，但保留正文、失败状态和真实图片。"""
import copy
import json

import pytest

from skill_toolbox.contracts.common import OperationResult
from skill_toolbox.resume_task import operation_result
from skill_toolbox.tools.resume_workflow import compact_agent_result


@pytest.mark.parametrize("ok", [True, False])
def test_compact_wire_keeps_candidate_content_images_and_failure(monkeypatch, ok):
    body = "保留全部职责与量化成果。" * 80
    result = OperationResult(
        ok=ok, artifact_id="resume-test", revision=2,
        status="validated" if ok else "failed",
        issues=[] if ok else [{"code": "PAGE_TARGET_EXCEEDED", "message": "实际2页"}],
        images=[{"media_type": "image/png", "base64_data": "image-data"}],
        data={"candidate_id": "resume-test@2", "content": {
            "sections": [{"key": "projects", "title": "项目", "entries": [
                {"id": "p1", "text": [body], "font_size_pt": None, "scale": None}]}]},
            "page_count": 2, "fits_page_target": False, "remaining_pages": [],
            "docx": "work/resume/test.docx", "pdf": "work/resume/test.pdf",
            "actual_changes": [{"text": body}], "applied_changes": [{"entry": {"text": body}}]},
    )
    original = copy.deepcopy(result.data)
    output = operation_result('resume_edit', result)
    text, images = output.content, [i.model_dump() for i in output.images]
    payload = json.loads(text)
    assert payload["data"]["content"]["sections"][0]["entries"][0]["text"] == [body]
    assert payload["data"]["candidate_id"] == "resume-test@2"
    assert payload["data"]["remaining_pages"] == []
    assert images == result.images and output.success is ok
    if not ok:
        assert payload["code"] == "PAGE_TARGET_EXCEEDED"
    assert result.data == original
    assert len(text) < len(json.dumps(result.as_dict(), ensure_ascii=False, indent=2)) * 0.6


def test_prepare_preserves_entire_source_and_accept_does_not_echo_resume():
    text = "原材料完整内容" * 100
    prepared = OperationResult(ok=True, data={"materials": [{"name": "材料", "assets": [],
        "blocks": [{"id": "b1", "type": "paragraph", "text": text, "source_locator": "xml/path",
                    "bbox": None, "page": 1, "asset_ids": []}]}]})
    view = compact_agent_result("resume_prepare", prepared)
    assert view.data["materials"][0]["blocks"] == [
        {"id": "b1", "type": "paragraph", "text": text, "page": 1, "asset_ids": []}]
    accepted = OperationResult(ok=True, data={"candidate_id": "r@1", "accepted_revision": 1,
        "docx": "work/resume/r.docx", "pdf": "work/resume/r.pdf", "content": {"text": text}})
    result = compact_agent_result("resume_accept", accepted)
    assert result.data["docx"] == accepted.data["docx"]
    assert result.data["accepted_revision"] == 1 and "content" not in result.data
