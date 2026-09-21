"""Semantic resume operations retain version and content guarantees without Word."""
import json
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from skill_toolbox.contracts.common import OperationResult, ToolError
from skill_toolbox.contracts.resume_workflow import (
    AcceptRequest, EditRequest, EntryPatch, GenerateRequest, PrepareRequest, PreviewRequest,
)
from skill_toolbox.llm_tools.dispatcher import DomainServices, dispatch_with_media
from skill_toolbox.materials import MaterialService
from skill_toolbox.resume_layout.t109 import TEMPLATE
from skill_toolbox.runtime import TaskRequest, _domain_tool_specs, _resume_workflow_enabled
from skill_toolbox.tools.materials import MaterialPlanService
from skill_toolbox.tools.resume_edit import ResumeEditService
from skill_toolbox.tools.resume_workflow import ResumeWorkflow


@pytest.fixture(params=["t109", "t001"])
def workflow(tmp_path, monkeypatch, request):
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (10, 12), "blue").save(source / "photo.png")
    (source / "resume.md").write_text("# 原简历\n\n" + "完整经历" * 100 + "\n\n![照片](photo.png)", encoding="utf-8")
    material_service = MaterialService(tmp_path)
    material_service.prepare_material(source / "resume.md")
    material = MaterialPlanService(tmp_path, material_service.catalog(), "resume", {"vision": True})
    engine = ResumeEditService(tmp_path, TEMPLATE.parent.parent, {"vision": True}, template_id=request.param)
    engine.test_pages = 1
    engine.test_mechanical = True
    engine.test_render_count = 0

    def render(self, artifact_id, revision, content, *, layout_mode, base_revision, changes, rev_dir):
        self.test_render_count += 1
        render_dir = rev_dir / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for n in range(self.test_pages):
            page = render_dir / f"page-{n + 1}.png"
            Image.new("RGB", (10, 12), "white").save(page)
            pages.append(self._rel(page))
        docx = rev_dir / "resume.docx"
        docx.write_bytes(b"fake document")
        record = {
            "artifact_id": artifact_id, "revision": revision, "parent": base_revision,
            "layout_mode": layout_mode, "changes": changes, "content": content,
            "content_hash": "stub", "page_count": self.test_pages,
            "mechanical": {"passed": self.test_mechanical, "errors": [] if self.test_mechanical else ["overlap"]},
            "visual": {"status": "pending", "delivered_pages": []},
            "docx": self._rel(docx), "pdf": None, "pages": pages,
            "docx_sha256": "stub", "pdf_sha256": None,
            "instance_model": [], "renderer": "stub",
        }
        (rev_dir / "revision.json").write_text(json.dumps(record), encoding="utf-8")
        self._write_qa_report(revision, {"passed": self.test_mechanical, "issues": []})
        return record

    monkeypatch.setattr(ResumeEditService, "_render_revision", render)
    monkeypatch.setattr(ResumeEditService, "prepare", lambda self, template_id: OperationResult(
        ok=True, data={"header_fields": ["name", "phone", "email"]}))
    return ResumeWorkflow(engine, material, request.param)


def generate(workflow, **overrides):
    content = {
        "person": {"name": "候选人", "phone": "123"},
        "sections": [
            {"key": "internship", "title": "实习经历", "entries": [
                {"id": "job-1", "date": "2024", "organization": "公司", "role": "实习生", "text": ["原职责", "原成果"]},
            ]},
            {"key": "skills", "title": "专业技能", "entries": [
                {"id": "skill-1", "text": ["Python", "SQL"]},
            ]},
        ],
    }
    content.update(overrides)
    return workflow.generate(GenerateRequest(content=content))


def test_candidate_recovery_reports_real_ids_without_accepting_filename(workflow):
    assert workflow.candidate_state()['candidate_ids'] == []
    with pytest.raises(ToolError) as missing:
        workflow._candidate('resume.resume_pro')
    assert 'resume_generate' in missing.value.suggestion
    result = generate(workflow)
    assert workflow.prepare(PrepareRequest()).data['candidate_state']['candidate_ids'] == [result.data['candidate_id']]
    with pytest.raises(ToolError) as invalid:
        workflow._candidate('resume.resume_pro')
    assert result.data['candidate_id'] in invalid.value.suggestion


@pytest.mark.asyncio
async def test_internal_candidate_question_never_opens_user_dialog(workflow):
    from skill_toolbox.models import ToolCall
    from skill_toolbox.providers.mock import ScriptedProvider
    from skill_toolbox.runtime import AgentRuntime, UserInputBroker
    events = []
    runtime = AgentRuntime(ScriptedProvider([]), events.append, input_broker=UserInputBroker())
    result = await runtime._execute_call(ToolCall(id='q', name='ask_user_questions', arguments={
        'questions': [{'id': 'candidate', 'label': '请提供 candidate_id'}]}), None,
        domain_services=DomainServices(resume_workflow=workflow))
    assert not result.success and 'INTERNAL_STATE_QUESTION' in result.content
    assert not any(event['type'] == 'questions_requested' for event in events)


def test_fact_references_survive_partial_edit_and_unknown_ref_fails_before_render(workflow):
    workflow.facts.add("request", "课程项目，完成功能测试")
    workflow.facts.save()
    result = generate(workflow, sections=[{"key": "projects", "title": "项目经历", "entries": [
        {"id": "p1", "text": ["完成功能测试"], "source_ids": ["request"]}]}])
    edited = workflow.edit(EditRequest(candidate_id=result.data["candidate_id"], changes=[
        {"op": "update_entry", "target_id": "projects#p1", "entry": {"text": ["设计测试用例并完成功能测试"]}}]))
    assert edited.data["content"]["sections"][0]["entries"][0]["source_ids"] == ["request"]
    assert edited.data["content_review"]["semantic_review"] == "agent_required"
    renders = workflow.engine.test_render_count
    with pytest.raises(ToolError, match="来源 ID"):
        workflow.edit(EditRequest(candidate_id=edited.data["candidate_id"], changes=[
            {"op": "update_entry", "target_id": "projects#p1", "entry": {"source_ids": ["bogus"]}}]))
    assert workflow.engine.test_render_count == renders


def test_plain_section_repeated_heading_is_normalized(workflow):
    result = generate(workflow, sections=[{"key": "skills", "title": "技能", "entries": [
        {"organization": "技能", "text": ["Python、SQL"]}]}])
    entry = result.data["content"]["sections"][0]["entries"][0]
    assert entry["organization"] == "" and entry["text"] == ["Python、SQL"]


def test_replace_last_entry_in_one_batch_keeps_section_available(workflow):
    result = generate(workflow)
    edited = workflow.edit(EditRequest(candidate_id=result.data["candidate_id"], changes=[
        {"op": "remove_entry", "target_id": "skills#skill-1"},
        {"op": "insert_entry", "section_id": "skills", "entry": {"text": ["Git"]}},
    ]))
    assert edited.ok
    skills = next(s for s in edited.data["content"]["sections"] if s["key"] == "skills")
    assert len(skills["entries"]) == 1 and skills["entries"][0]["text"] == ["Git"]


def test_prepare_returns_whole_source_and_no_photo_hides_template_sample(workflow):
    prepared = workflow.prepare(PrepareRequest()).data
    ir = next(iter(workflow.materials.catalog.irs.values()))
    blocks = prepared["materials"][0]["blocks"]
    assert [{k: v for k, v in block.items() if k != "source_id"} for block in blocks] == [b.model_dump() for b in ir.blocks]
    assert all(workflow.facts.sources[b["source_id"]]["text"] == b["text"] for b in blocks)
    assert prepared["template_id"] == workflow.template_id
    result = generate(workflow)
    record = workflow.engine.store.load_revision(result.artifact_id, result.revision)
    assert record["content"]["header"]["hide_photo"] is True
    assert record["content"]["header"]["photo_path"] == ""
    assert record["content"]["header"]["fields"]["email"] == ""


def test_scaled_font_rejected_before_word_and_without_revision_change(workflow):
    with pytest.raises(ToolError, match="8pt") as exc:
        generate(workflow, sections=[{"key": "skills", "title": "技能", "scale": 0.9,
                                     "entries": [{"text": ["Python"], "font_size_pt": 8}]}])
    assert exc.value.code == "TYPOGRAPHY_BOUNDS"
    assert workflow.engine.test_render_count == 0
    first = generate(workflow)
    with pytest.raises(ToolError, match="8pt"):
        workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
            {"op": "format", "scope": "all", "font_size_pt": 8, "scale": 0.9}]))
    assert workflow.engine.test_render_count == 1
    assert workflow.engine.store.index(first.artifact_id)["current"] == first.revision


def test_density_edit_keeps_content_and_expands_uniformly_scaled_width(workflow):
    first = generate(workflow)
    scaled = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
        {"op": "format", "scope": "all", "font_size_pt": 10, "scale": 0.9}]))
    compact = workflow.edit(EditRequest(candidate_id=scaled.data["candidate_id"], density="compact"))
    assert compact.data["density"] == "compact"
    for old, new in zip(first.data["content"]["sections"], compact.data["content"]["sections"]):
        assert new["scale"] == 1
        for before, after in zip(old["entries"], new["entries"]):
            assert after["text"] == before["text"] and after["organization"] == before["organization"]
            assert after["font_size_pt"] == 9 and after["scale"] == 1
    assert compact.data["content"]["person"] == first.data["content"]["person"]


def test_move_section_preserves_all_entry_content(workflow):
    first = generate(workflow)
    result = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[{
        "op": "move_section", "section_id": "skills", "before_id": "internship",
    }]))
    assert result.artifact_id == first.artifact_id
    assert result.data["content"]["sections"] == list(reversed(first.data["content"]["sections"]))


def test_personal_component_add_rename_remove_retains_body(workflow):
    first = generate(workflow)
    added = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
        {"op": "set_person_field", "field": {"key": "github", "label": "GitHub", "value": "example.com/demo"}},
        {"op": "set_person_field", "field": {"key": "phone", "label": "联系电话", "value": "123"}},
    ]))
    assert added.data["content"]["sections"] == first.data["content"]["sections"]
    assert {f["key"] for f in added.data["content"]["personal_fields"]} == {"github", "phone"}
    changed = workflow.edit(EditRequest(candidate_id=added.data["candidate_id"], changes=[
        {"op": "update_person", "fields": {"github": "example.com/new"}},
        {"op": "remove_person_field", "key": "phone"},
    ]))
    assert changed.data["content"]["personal_fields"] == [{"key": "github", "label": "GitHub", "value": "example.com/new", "emphasis": "normal", "link": "", "background": False}]
    assert "phone" not in changed.data["content"]["person"]
    assert changed.data["content"]["sections"] == first.data["content"]["sections"]


def test_project_details_survive_text_only_edit(workflow):
    first = generate(workflow)
    target = first.data['content']['sections'][0]['entries'][0]['id']
    fields = [{'label':'Stars','value':'120','link':'https://github.com/example/demo','emphasis':'bold_accent','background':False,'layout':'auto'}]
    added = workflow.edit(EditRequest(candidate_id=first.data['candidate_id'], changes=[
        {'op':'update_entry','target_id':target,'entry':{'details':fields}}]))
    assert added.data['content']['sections'][0]['entries'][0]['details'] == fields
    edited = workflow.edit(EditRequest(candidate_id=added.data['candidate_id'], changes=[
        {'op':'update_entry','target_id':target,'entry':{'text':['只修改这段正文']}}]))
    assert edited.data['content']['sections'][0]['entries'][0]['details'] == fields


@pytest.mark.parametrize("target_id", ["job-1", "internship#job-1"])
def test_update_preserves_unspecified_fields_and_retry_is_idempotent(workflow, target_id):
    first = generate(workflow)
    request = EditRequest(candidate_id=first.data["candidate_id"], changes=[{
        "op": "update_entry", "target_id": target_id, "entry": {"role": "分析师"},
    }])
    edited = workflow.edit(request)
    content = edited.data["content"]
    job = content["sections"][0]["entries"][0]
    assert (job["date"], job["organization"], job["role"], job["text"]) == ("2024", "公司", "分析师", ["原职责", "原成果"])
    assert content["person"] == first.data["content"]["person"]
    assert content["sections"][1] == first.data["content"]["sections"][1]
    repeated = workflow.edit(request)
    assert repeated.data["candidate_id"] == edited.data["candidate_id"]
    assert workflow.engine.test_render_count == 2


def test_replace_photo_keeps_artifact_and_all_semantic_content(workflow):
    first = generate(workflow)
    asset = next(iter(workflow.materials.catalog.irs.values())).assets[0]
    edited = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[{
        "op": "replace_photo", "asset_id": asset.id,
    }]))
    assert edited.artifact_id == first.artifact_id
    assert edited.revision == first.revision + 1
    assert edited.data["content"]["sections"] == first.data["content"]["sections"]
    assert edited.data["content"]["person"] == first.data["content"]["person"]
    record = workflow.engine.store.load_revision(edited.artifact_id, edited.revision)
    assert record["content"]["header"]["photo_path"] == asset.path
    assert record["content"]["header"]["hide_photo"] is False
    plan = json.loads((workflow.engine.workspace / "work/plans/content-plan.json").read_text(encoding="utf-8"))
    assert asset.id in {selection["source_id"] for selection in plan["selections"]}
    assert asset.id not in {exclusion["source_id"] for exclusion in plan["exclusions"]}


def test_failed_candidate_keeps_repair_reference_through_dispatch(workflow):
    first = generate(workflow)
    workflow.engine.test_mechanical = False
    text, images, meta = dispatch_with_media("resume_edit", {
        "candidate_id": first.data["candidate_id"],
        "changes": [{"op": "update_entry", "target_id": "internship#job-1", "entry": {"role": "分析师"}}],
    }, DomainServices(resume_workflow=workflow))
    payload = json.loads(text)
    assert not meta["ok"]
    assert payload["data"]["candidate_id"] == f"{first.artifact_id}@2"
    assert payload["data"]["content"]["sections"][0]["entries"][0]["role"] == "分析师"
    assert images


def test_page_target_blocks_accept_even_with_passing_mechanical_qa(workflow):
    workflow.engine.test_pages = 2
    candidate = generate(workflow)
    assert not candidate.ok
    assert not candidate.data["fits_page_target"]
    assert candidate.data["candidate_id"]
    with pytest.raises(ToolError) as error:
        workflow.accept(AcceptRequest(candidate_id=candidate.data["candidate_id"], visual_notes="两页均检查"))
    assert error.value.code == "PAGE_TARGET_EXCEEDED"
    assert workflow.engine.store.index(candidate.artifact_id)["accepted"] == 0


def test_page_failure_leads_with_measured_reduction_budget(workflow):
    workflow.engine.test_pages = 2
    candidate = generate(workflow)
    path = workflow.engine.store.revision_dir(candidate.artifact_id, candidate.revision) / "revision.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["single_page_fit"] = {"required_reduction_pt": 81.0, "approx_lines_to_save": 6,
                                 "reference_line_pitch_pt": 13.5}
    path.write_text(json.dumps(record), encoding="utf-8")
    result = workflow._result(OperationResult(ok=True, artifact_id=candidate.artifact_id,
                                             revision=candidate.revision))
    assert result.next_action.startswith("当前整页还需节省约 81.0pt（约 6 行正文高度）")
    assert not result.ok
    assert result.data["page_fit"]["required_reduction_pt"] == 81.0


@pytest.mark.parametrize("style_patch", [{}, {"font_size_pt": None, "scale": None}])
def test_font_defaults_and_text_edit_preserve_explicit_typography(workflow, style_patch):
    content = {"sections": [{"key": "work", "title": "工作经历", "scale": 0.95, "entries": [
        {"id": "one", "text": ["第一条"], "font_size_pt": 11, "scale": 0.9},
        {"id": "two", "text": ["第二条"]},
    ]}]}
    first = workflow.generate(GenerateRequest(content=content, font_size_pt=10))
    section = first.data["content"]["sections"][0]
    assert section["scale"] == 0.95
    assert [entry["font_size_pt"] for entry in section["entries"]] == [11, 10]
    edited = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
        {"op": "update_entry", "target_id": "one", "entry": {"text": ["更新正文"], **style_patch}},
    ]))
    section = edited.data["content"]["sections"][0]
    assert section["scale"] == 0.95
    assert section["entries"][0]["scale"] == 0.9
    assert section["entries"][0]["font_size_pt"] == 11


def test_format_scopes_set_absolute_values_and_preserve_other_entries(workflow):
    first = generate(workflow)
    second = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
        {"op": "format", "scope": "all", "font_size_pt": 10, "scale": 0.9},
        {"op": "format", "scope": "entry", "target_id": "job-1", "font_size_pt": 11, "scale": 0.95},
    ]))
    sections = second.data["content"]["sections"]
    assert all(s["scale"] == 0.9 for s in sections)
    assert sections[0]["entries"][0]["font_size_pt"] == 11
    assert sections[1]["entries"][0]["font_size_pt"] == 10
    third = workflow.edit(EditRequest(candidate_id=second.data["candidate_id"], changes=[
        {"op": "format", "scope": "section", "target_id": "internship", "scale": 0.9},
    ]))
    assert third.data["content"]["sections"] == sections


def test_page_only_edit_reuses_candidate_and_rejects_stale_target_change(workflow):
    workflow.engine.test_pages = 2
    first = generate(workflow)
    edited = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], target_pages=2))
    assert edited.ok
    assert edited.data["candidate_id"] == first.data["candidate_id"]
    assert edited.data["target_pages"] == 2
    assert workflow.engine.test_render_count == 1
    newer = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
        {"op": "update_person", "fields": {"phone": "456"}},
    ]))
    with pytest.raises(ToolError, match="最新"):
        workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], target_pages=3))
    assert workflow._target(newer.artifact_id) == 2


def test_failed_edit_does_not_change_page_target(workflow):
    first = generate(workflow)
    workflow.engine.test_mechanical = False
    result = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], target_pages=3, changes=[
        {"op": "update_person", "fields": {"phone": "456"}},
    ]))
    assert not result.ok
    assert workflow._target(first.artifact_id) == 1


def test_old_edit_and_generation_retries_preserve_newer_page_target(workflow):
    first = generate(workflow)
    edit = EditRequest(candidate_id=first.data["candidate_id"], target_pages=2, changes=[
        {"op": "update_person", "fields": {"phone": "456"}},
    ])
    second = workflow.edit(edit)
    workflow.edit(EditRequest(candidate_id=second.data["candidate_id"], target_pages=3))
    assert workflow.edit(edit).data["target_pages"] == 3
    assert generate(workflow).data["target_pages"] == 3
    assert workflow.engine.test_render_count == 2


def test_delivery_requires_accept_even_after_all_pages_previewed(workflow):
    first = generate(workflow)
    workflow.preview(PreviewRequest(candidate_id=first.data["candidate_id"]))
    with pytest.raises(ValueError, match="resume_accept"):
        workflow.verify_delivery([first.data["docx"]])
    accepted = workflow.accept(AcceptRequest(candidate_id=first.data["candidate_id"], visual_notes="页面已检查"))
    workflow.verify_delivery([accepted.data["docx"]])
    workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[
        {"op": "update_person", "fields": {"phone": "456"}},
    ]))
    with pytest.raises(ValueError, match="最新"):
        workflow.verify_delivery([accepted.data["docx"]])


def test_delivery_rejects_mixed_artifacts_and_lowered_page_limit(workflow):
    first = generate(workflow)
    accepted = workflow.accept(AcceptRequest(candidate_id=first.data["candidate_id"], visual_notes="页面已检查"))
    second = generate(workflow, person={"name": "另一人"})
    with pytest.raises(ValueError, match="同一"):
        workflow.verify_delivery([accepted.data["docx"], second.data["docx"]])
    workflow.engine.test_pages = 2
    large = workflow.edit(EditRequest(candidate_id=second.data["candidate_id"], target_pages=2, changes=[
        {"op": "update_person", "fields": {"phone": "456"}},
    ]))
    workflow.accept(AcceptRequest(candidate_id=large.data["candidate_id"], visual_notes="两页已检查"))
    workflow.edit(EditRequest(candidate_id=large.data["candidate_id"], target_pages=1))
    with pytest.raises(ValueError, match="允许页数"):
        workflow.verify_delivery([large.data["docx"]])


def test_preview_and_accept_are_bound_to_candidate_revision(workflow):
    first = generate(workflow)
    second = workflow.edit(EditRequest(candidate_id=first.data["candidate_id"], changes=[{
        "op": "update_person", "fields": {"phone": "456"},
    }]))
    preview = workflow.preview(PreviewRequest(candidate_id=first.data["candidate_id"]))
    assert preview.data["candidate_id"] == first.data["candidate_id"]
    assert preview.data["content"]["person"]["phone"] == "123"
    with pytest.raises(ToolError) as error:
        workflow.accept(AcceptRequest(candidate_id=first.data["candidate_id"], visual_notes="旧版已检查"))
    assert error.value.code == "STALE_CANDIDATE"
    accepted = workflow.accept(AcceptRequest(candidate_id=second.data["candidate_id"], visual_notes="新版已检查"))
    assert accepted.ok
    assert workflow.engine.store.index(second.artifact_id)["accepted"] == second.revision


def test_accept_retry_replays_same_candidate(workflow):
    first = generate(workflow)
    request = AcceptRequest(candidate_id=first.data["candidate_id"], visual_notes="全部页面已检查")
    accepted = workflow.accept(request)
    repeated = workflow.accept(request)
    assert repeated.data["candidate_id"] == accepted.data["candidate_id"]


@pytest.mark.parametrize("template_id", ["t001", "t109"])
def test_template_bound_schema_hides_low_level_and_manual_plan(tmp_path, template_id):
    skill = SimpleNamespace(id="resume_pro", tool_mode="domain")
    request = TaskRequest(skill_id="resume_pro", user_prompt="制作简历", output_dir=tmp_path,
                          template_id=template_id, tool_mode="domain")
    assert _resume_workflow_enabled(skill, request)
    specs = _domain_tool_specs(skill, {"vision": True}, workflow=True)
    names = {spec["name"] for spec in specs}
    assert names & {"resume_prepare", "resume_generate", "resume_edit", "resume_preview", "resume_accept"} == {
        "resume_prepare", "resume_generate", "resume_edit", "resume_preview", "resume_accept"}
    assert not names & {"resume_prepare_v2", "resume_generate_v2", "resume_repair_v2", "create_content_plan", "resume_restore"}
    assert not any("template_id" in spec["input_schema"].get("properties", {}) for spec in specs if spec["name"].startswith("resume_"))


def test_update_entry_cannot_rename_identity():
    assert "id" not in EntryPatch.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        EditRequest(candidate_id="candidate@1", changes=[{
            "op": "update_entry", "target_id": "job-1", "entry": {"id": "another-job"},
        }])
