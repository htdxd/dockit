"""简历工作流公开 schema 与参数契约保持一致。"""

import json

import pytest
from pydantic import ValidationError

from skill_toolbox.contracts.resume_workflow import EditRequest, Entry, GenerateRequest
from skill_toolbox.llm_tools.resume_workflow import REQUEST_MODELS, workflow_tools


@pytest.mark.parametrize("name,payload", [
    ("resume_prepare", {}),
    ("resume_generate", {"content": {"sections": [
        {"key": "education", "title": "教育背景", "entries": [{"organization": "某大学"}]},
    ]}}),
    ("resume_edit", {"candidate_id": "a@1", "changes": [
        {"op": "update_entry", "target_id": "education#1", "entry": {"text": []}},
    ]}),
    ("resume_preview", {"candidate_id": "a@1", "pages": [1]}),
    ("resume_accept", {"candidate_id": "a@1", "visual_notes": "已逐页检查，没有文字遮挡"}),
])
def test_public_requests_roundtrip_through_their_schema_model(name, payload):
    tools = {tool["name"]: tool for tool in workflow_tools()}
    model = REQUEST_MODELS[name]
    request = model.model_validate(payload)
    assert model.model_validate(request.model_dump()) == request
    schema = tools[name]["input_schema"]
    source = model.model_json_schema()
    assert schema.get("required", []) == source.get("required", [])
    assert schema["properties"].keys() == source["properties"].keys()
    assert schema["additionalProperties"] is False
    assert '"$ref"' not in json.dumps(schema)


def test_partial_patch_retains_omitted_fields_and_can_clear_text():
    request = EditRequest.model_validate({"candidate_id": "a@1", "changes": [
        {"op": "update_entry", "target_id": "education#1", "entry": {"text": []}},
    ]})
    patch = request.changes[0].entry
    assert patch.model_fields_set == {"text"}
    assert patch.model_dump(exclude_unset=True) == {"text": []}


@pytest.mark.parametrize("payload", [{}, {"text": ["  "]}, {"id": "entry-1"}])
def test_full_entry_rejects_empty_content(payload):
    with pytest.raises(ValidationError, match="条目必须"):
        Entry.model_validate(payload)


def test_target_page_limit_and_unknown_geometry_rejected():
    content = {"sections": [{"key": "s", "title": "经历", "entries": [{"text": ["正文"]}]}]}
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate({"content": content, "target_pages": 4})
    with pytest.raises(ValidationError):
        EditRequest.model_validate({"candidate_id": "a@1", "changes": [
            {"op": "update_entry", "target_id": "s#1", "entry": {"dy_pt": 10}},
        ]})


def test_only_five_high_level_tools_exposed():
    assert [tool["name"] for tool in workflow_tools()] == [
        "resume_prepare", "resume_generate", "resume_edit", "resume_preview", "resume_accept",
    ]


def test_typography_bounds_and_page_only_edit():
    patch = EditRequest.model_validate({"candidate_id": "a@1", "target_pages": 3})
    assert patch.changes == []
    request = EditRequest.model_validate({"candidate_id": "a@1", "changes": [
        {"op": "format", "scope": "section", "target_id": "work", "font_size_pt": 10, "scale": 0.9},
    ]})
    assert request.changes[0].font_size_pt == 10
    for change in [
        {"op": "format", "scope": "all"},
        {"op": "format", "scope": "entry", "font_size_pt": 10},
        {"op": "format", "scope": "all", "scale": 0.2},
        {"op": "format", "scope": "all", "font_size_pt": 30},
    ]:
        with pytest.raises(ValidationError):
            EditRequest.model_validate({"candidate_id": "a@1", "changes": [change]})


def test_edit_requires_candidate_and_accepts_unambiguous_encoded_array():
    change = {"op": "format", "scope": "all", "font_size_pt": 10}
    with pytest.raises(ValidationError):
        EditRequest.model_validate({"changes": [change]})
    native = EditRequest.model_validate({"candidate_id": "a@1", "changes": [change]})
    encoded = EditRequest.model_validate({"candidate_id": "a@1", "changes": json.dumps([change])})
    assert native == encoded


def test_encoded_changes_reports_exact_bad_json_position_without_repair():
    value = '[{"op":"update_entry","target_id":"a","entry":{"text":["正文"]]}}]'
    with pytest.raises(json.JSONDecodeError) as parsing:
        json.loads(value)
    with pytest.raises(ValidationError) as invalid:
        EditRequest.model_validate({"candidate_id": "a@1", "changes": value})
    message = str(invalid.value)
    assert f"位置 {parsing.value.pos}" in message
    assert "每次一个动作" in message


@pytest.mark.parametrize("value", [
    '{"op":"remove_entry","target_id":"a"}',
    '"[]"',
    '[{"op":"update_entry","target_id":"a","entry":{"text":[["正文"]]}}]',
    '[{"op":"remove_entry","target_id":"a","unknown":"不能忽略"}]',
])
def test_encoded_changes_still_require_full_contract_validation(value):
    with pytest.raises(ValidationError):
        EditRequest.model_validate({"candidate_id": "a@1", "changes": value})


def test_schema_metadata_titles_removed_but_section_title_field_preserved():
    tools = {tool["name"]: tool["input_schema"] for tool in workflow_tools()}
    schema = tools["resume_generate"]
    sections = schema["properties"]["content"]["properties"]["sections"]
    assert "title" not in schema
    assert sections["type"] == "array"
    section = sections["items"]
    assert "title" in section["required"]
    assert section["properties"]["title"]["type"] == "string"
    assert "title" not in section["properties"]["title"]
