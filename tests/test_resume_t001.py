"""t001 与 t109 共用组件服务的回归。"""
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from resume_templates import body_for
from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.resume import (
    ResumeContentV2,
    ResumeEditV2,
    ResumeGenerateV2Request,
    ResumeHeaderV2,
    ResumePersonalField,
    ResumeRepairV2Request,
)
from skill_toolbox.resume_layout import emit, layout, pipeline
from skill_toolbox.tools.resume_edit import ResumeEditService

TEMPLATES = Path(__file__).resolve().parents[1] / "backend/skill_toolbox/skill_defs/resume_pro/templates"
TEMPLATE = TEMPLATES / "t001/template.docx"


def content():
    return ResumeContentV2(
        template_id="t001", header=ResumeHeaderV2(
            fields={"name": "测试姓名", "phone": "13800000000", "age": "23",
                    "degree": "本科", "target_role": "软件工程师", "email": "",
                    "wechat": "", "address": ""}, hide_photo=True),
        sections=[{"key": "internship", "title": "实习经历", "prototype": "experience_v1",
                   "entries": [{"id": "work-1", "head": {"role": "实习生", "org": "测试公司",
                                                       "date": "2025.01-2025.06"},
                                "bullets": ["实现业务功能，维护自动化测试。"]}]}],
    )


def test_explicit_heading_without_date_uses_heading_style():
    body = body_for(TEMPLATE, 'projects')
    emit.set_box_text(body, "模型推理项目\t独立负责\n实现模型推理服务", first_is_header=True)
    paragraphs = body.findall(".//" + emit.W + "txbxContent/" + emit.W + "p")
    assert paragraphs[0].find(emit.W + "pPr/" + emit.W + "numPr") is None
    assert paragraphs[1].find(emit.W + "pPr/" + emit.W + "numPr") is not None


def test_t001_photo_png_bytes_match_explicit_content_type(tmp_path):
    svc = ResumeEditService(tmp_path, TEMPLATES, template_id="t001")
    scenario = svc._scenario_from_content(content().model_dump())
    buffer = io.BytesIO()
    Image.new("RGB", (12, 18), (50, 120, 200)).save(buffer, format="PNG")
    png = buffer.getvalue()
    scenario["header"] = {"photo_bytes": png}
    plan = layout.plan_layout(scenario["sections"],
                              {"work-1": layout.MeasureResult("work-1", 2, 36)},
                              geometry=svc.geometry)
    out = tmp_path / "photo.docx"
    pipeline.emit_scenario(scenario, plan, out, template=TEMPLATE, template_id="t001")
    with zipfile.ZipFile(out) as package:
        assert package.read("word/media/image1.jpeg") == png
        types = emit.etree.fromstring(package.read("[Content_Types].xml"))
        override = next(e for e in types if e.get("PartName") == "/word/media/image1.jpeg")
        assert override.get("ContentType") == "image/png"


@pytest.mark.parametrize("template_id", ["t109", "t001"])
def test_plain_entry_preserves_added_header_and_existing_lines(tmp_path, template_id):
    svc = ResumeEditService(tmp_path, TEMPLATES, template_id=template_id)
    text = svc._entry_text(
        {"prototype": "plain_lines_v1"},
        {"head": {"org": "某某大学", "date": "2021-2025"}, "lines": ["主修软件工程"]},
    )
    assert "某某大学" in text and "2021-2025" in text
    assert text.endswith("\n主修软件工程")


def test_t001_xml_emit_removes_original_bodies_and_photo(tmp_path):
    svc = ResumeEditService(tmp_path, TEMPLATES, template_id="t001")
    data = content().model_dump()
    scenario = svc._scenario_from_content(data)
    scenario["header"] = svc._header_payload(data)
    plan = layout.plan_layout(scenario["sections"],
                              {"work-1": layout.MeasureResult("work-1", 2, 36)},
                              geometry=svc.geometry)
    out = tmp_path / "resume.docx"
    pipeline.emit_scenario(scenario, plan, out, template=TEMPLATE, template_id="t001")
    root = emit.load_document_xml(out)
    # 当前与 t109 一样以 Word Choice 分支验收，VML fallback 仍属既有兼容限制。
    text = "".join(t.text or "" for box in root.iter(emit.WPS + "wsp")
                   for t in box.iter(emit.W + "t"))
    assert "测试姓名" in text and "测试公司" in text
    assert "贵泽实业" not in text and "一路网络" not in text
    assert root.find(".//{http://schemas.openxmlformats.org/drawingml/2006/picture}pic") is None
    assert "年龄：23" in "".join(text.split())


def _stub_render(self, artifact_id, revision, content, *, rev_dir, **kwargs):
    record = {
        "artifact_id": artifact_id, "revision": revision, "content": content,
        "page_count": 1, "mechanical": {"passed": True, "errors": []},
        "visual": {"status": "pending", "delivered_pages": []},
        "pages": [], "docx": "resume.docx", "pdf": None, "changes": kwargs["changes"],
    }
    (rev_dir / "revision.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def test_header_only_revision_preserves_content_and_merges_explicit_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(ResumeEditService, "_render_revision", _stub_render)
    svc = ResumeEditService(tmp_path, TEMPLATES, template_id="t001")
    original = svc.generate(ResumeGenerateV2Request(template_id="t001", content=content(),
                                                   request_id="create"))
    request = ResumeRepairV2Request(
        artifact_id=original.artifact_id, base_revision=1,
        header=ResumeHeaderV2(fields={"name": "新姓名"}), request_id="header")
    changed = svc.repair(request)
    assert changed.artifact_id == original.artifact_id and changed.revision == 2
    stored = svc.store.load_revision(changed.artifact_id, 2)["content"]
    assert stored["sections"] == content().model_dump()["sections"]
    assert stored["header"]["fields"]["name"] == "新姓名"
    assert stored["header"]["fields"]["phone"] == "13800000000"
    assert stored["header"]["hide_photo"] is True
    assert svc.repair(request).revision == 2
    third = svc.repair(ResumeRepairV2Request(
        artifact_id=original.artifact_id, base_revision=2,
        header=ResumeHeaderV2(custom_fields=[
            ResumePersonalField(key="github", label="GitHub", value="https://github.com/example"),
        ]), request_id="custom-add"))
    fourth = svc.repair(ResumeRepairV2Request(
        artifact_id=original.artifact_id, base_revision=third.revision,
        header=ResumeHeaderV2(custom_fields=[], hidden_fields=["degree"]), request_id="custom-remove"))
    final = svc.store.load_revision(original.artifact_id, fourth.revision)["content"]
    assert final["header"]["custom_fields"] == []
    assert final["header"]["hidden_fields"] == ["degree"]
    assert final["header"]["fields"]["phone"] == "13800000000"


def test_insert_section_before_is_explicit_and_missing_target_is_rejected(tmp_path):
    svc = ResumeEditService(tmp_path, TEMPLATES, template_id="t001")
    original = content().model_dump()
    section = {"key": "projects", "title": "项目经历", "entries": []}
    changed, _ = svc.actions.apply(original, [
        ResumeEditV2(op="insert_section", section=section, before="internship"),
    ])
    assert [s["key"] for s in changed["sections"]] == ["projects", "internship"]
    with pytest.raises(ToolError) as error:
        svc.actions.apply(original, [
            ResumeEditV2(op="insert_section", section=section, after="missing"),
        ])
    assert error.value.code == "TARGET_NOT_FOUND"
    assert [s["key"] for s in original["sections"]] == ["internship"]
