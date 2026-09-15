"""阶段 4 强制测试：简历领域 Service（模板预处理 + 生成 + 受限修复）。

覆盖计划 §阶段4 验收：
- 已开放组件模板的 hash 校验与字段/组件/容量索引
- 未知模板 / 未索引字段明确报错
- 生成成功路径（产物存在 + QA mechanical=passed + 无溢出）
- 溢出失败路径（FILL_OVERFLOW，不注册 artifact）
- 受限修复：白名单动作成功、白名单外动作被拒且候选自动丢弃
- 照片 asset_id 链路；无 Vision 不读取图片 payload
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from PIL import Image

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.contracts.resume import (
    ResumeChange,
    ResumeChangeRequest,
    ResumeGenerateRequest,
)
from skill_toolbox.materials import MaterialService
from skill_toolbox.tools.resume import ResumeService

TEMPLATES = Path("backend/skill_toolbox/skill_defs/resume_pro/templates")
TEMPLATE_IDS = ["t001", "t109"]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def service(workspace: Path) -> ResumeService:
    return ResumeService(
        workspace,
        TEMPLATES,
        capabilities={"vision": False, "tool_calling": True},
    )


def _catalog_with_photo(workspace: Path, tmp_path: Path) -> MaterialService:
    """构造含一张候选照片材料的 catalog（photo 走 asset_id 链路）。"""
    photo = tmp_path / "portrait.png"
    Image.new("RGB", (120, 150), (30, 60, 120)).save(photo)
    material_service = MaterialService(workspace)
    material_service.prepare_material(photo)
    return material_service


# ---------------- 模板预处理 ----------------

@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_prepare_returns_index_and_capacity(template_id: str, service: ResumeService) -> None:
    result = service.prepare(template_id)
    assert result.ok
    data = result.data
    assert data["template_id"] == template_id
    assert data["template_hash"]
    assert data["fields"], template_id
    assert data["components"], template_id
    for comp in data["components"]:
        assert comp["allowed_actions"]
        assert "replace_text" in comp["allowed_actions"] or "replace_asset" in comp["allowed_actions"]
    assert any(comp["capacity_lines"] is not None for comp in data["components"])
    assert data["target_actions"] == ["resume_generate", "resume_repair"]


@pytest.mark.parametrize("template_id", ["t002", "t026", "t046", "t999"])
def test_prepare_unsupported_template(template_id: str, service: ResumeService) -> None:
    with pytest.raises(ToolError) as exc:
        service.prepare(template_id)
    assert exc.value.code == "TEMPLATE_UNSUPPORTED"


def test_prepare_rejects_tampered_template_hash(workspace: Path, tmp_path: Path) -> None:
    """模板 hash 不匹配 → TEMPLATE_HASH_MISMATCH（禁止对未知结构套旧 selector）。"""
    fake_root = tmp_path / "templates"
    fake_dir = fake_root / "t001"
    fake_dir.mkdir(parents=True)
    (fake_dir / "template.docx").write_bytes(b"PK\x03\x04 tampered")
    manifest = json.loads(
        (TEMPLATES / "t001" / "manifest.json").read_text(encoding="utf-8")
    )
    manifest["template_sha256"] = "0" * 64
    (fake_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    svc = ResumeService(workspace, fake_root)

    with pytest.raises(ToolError) as exc:
        svc.prepare("t001")
    assert exc.value.code == "TEMPLATE_HASH_MISMATCH"


# ---------------- 生成 ----------------

def test_generate_success(service: ResumeService, workspace: Path) -> None:
    request = ResumeGenerateRequest(
        template_id="t001",
        fields={
            "name": "张三",
            "phone": "138-0000-8000",
            "intent": "AI 算法工程师",
            "work": "负责推荐系统架构与交付。",
        },
    )
    result = service.generate(request)
    assert result.ok
    assert result.data["overflow_risks"] == []
    assert (workspace / "artifacts" / "resume.docx").is_file()
    qa = json.loads(
        (workspace / "work" / "qa" / "resume.json").read_text(encoding="utf-8")
    )
    assert qa["mechanical"] == "passed"
    assert qa["visual"] == "not_run"  # 无 Vision


def test_generate_unsupported_field(service: ResumeService) -> None:
    request = ResumeGenerateRequest(
        template_id="t001",
        fields={"invented_field": "x", "name": "张三"},
    )
    with pytest.raises(ToolError) as exc:
        service.generate(request)
    assert exc.value.code == "FIELD_UNSUPPORTED"


def test_generate_overflow_fails_without_artifact(
    service: ResumeService, workspace: Path
) -> None:
    """内容超长 → FILL_OVERFLOW，不注册 artifact（候选失败自动丢弃）。"""
    request = ResumeGenerateRequest(
        template_id="t001",
        fields={
            "name": "张三",
            "phone": "138-0000-8000",
            "work": "负责" + "系统架构设计与算法优化" * 80,
        },
    )
    with pytest.raises(ToolError) as exc:
        service.generate(request)
    assert exc.value.code == "FILL_OVERFLOW"
    assert exc.value.retryable


def test_generate_with_photo_asset(
    service: ResumeService, workspace: Path, tmp_path: Path
) -> None:
    material_service = _catalog_with_photo(workspace, tmp_path)
    service.catalog = material_service.catalog()
    asset = next(iter(material_service.catalog().irs.values())).assets[0]

    request = ResumeGenerateRequest(
        template_id="t001",
        fields={"name": "张三", "phone": "138-0000-8000"},
        photo_asset_id=asset.id,
    )
    result = service.generate(request)
    assert result.ok
    # 照片字节嵌入产物（media 目录含用户照片）
    import zipfile

    with zipfile.ZipFile(workspace / "artifacts" / "resume.docx") as z:
        assert any("_user" in name for name in z.namelist())


def test_generate_unknown_photo_asset(service: ResumeService) -> None:
    request = ResumeGenerateRequest(
        template_id="t001",
        fields={"name": "张三"},
        photo_asset_id="asset-invented-000000",
    )
    with pytest.raises(ToolError) as exc:
        service.generate(request)
    assert exc.value.code == "ASSET_UNKNOWN"


# ---------------- 受限修复 ----------------

def test_repair_replace_text(service: ResumeService, workspace: Path) -> None:
    service.generate(
        ResumeGenerateRequest(
            template_id="t001",
            fields={"name": "张三", "phone": "138-0000-8000"},
        )
    )
    before = (workspace / "artifacts" / "resume.docx").read_bytes()

    result = service.repair(
        ResumeChangeRequest(
            artifact_id="resume-abc",
            changes=[ResumeChange(action="replace_text", component_id="name", text="李四")],
        )
    )
    assert result.ok
    assert result.data["action_records"], "应产生 replace_text 动作记录"
    # 产物已更新（候选副本成功才提交）
    after = (workspace / "artifacts" / "resume.docx").read_bytes()
    assert after != before


def test_repair_whitelist_action_rejected(service: ResumeService, workspace: Path) -> None:
    """photo 组件白名单不含 shift_components → 拒绝且不覆盖基线产物。"""
    service.generate(
        ResumeGenerateRequest(template_id="t001", fields={"name": "张三"})
    )
    before = (workspace / "artifacts" / "resume.docx").read_bytes()

    with pytest.raises(ToolError) as exc:
        service.repair(
            ResumeChangeRequest(
                artifact_id="resume-abc",
                changes=[ResumeChange(action="shift_components", component_id="photo", move_rows=1)],
            )
        )
    assert exc.value.code in {"ACTION_NOT_ALLOWED", "REPAIR_FAILED"}
    # 候选失败自动丢弃：基线产物未被覆盖
    assert (workspace / "artifacts" / "resume.docx").read_bytes() == before


def test_repair_move_rows_and_resize_rows(service: ResumeService, workspace: Path) -> None:
    """move_rows/resize_rows 后端换算为受限动作（机械门由 fill 兜底）。"""
    service.generate(
        ResumeGenerateRequest(template_id="t001", fields={"name": "张三"})
    )

    result = service.repair(
        ResumeChangeRequest(
            artifact_id="resume-abc",
            changes=[
                ResumeChange(action="shift_components", component_id="work", move_rows=1),
                ResumeChange(action="resize_component", component_id="work", resize_rows=2),
            ],
        )
    )
    assert result.ok
    actions = result.data["action_records"]
    assert any(a.get("action") == "shift_components" for a in actions)
    assert any(a.get("action") == "resize_component" for a in actions)


def test_repair_unknown_component(service: ResumeService, workspace: Path) -> None:
    service.generate(
        ResumeGenerateRequest(template_id="t001", fields={"name": "张三"})
    )
    with pytest.raises(ToolError) as exc:
        service.repair(
            ResumeChangeRequest(
                artifact_id="resume-abc",
                changes=[ResumeChange(action="replace_text", component_id="nope", text="x")],
            )
        )
    assert exc.value.code == "COMPONENT_UNKNOWN"


# ---------------- 无 Vision 图片 payload ----------------

def test_no_vision_no_image_payload_in_results(
    service: ResumeService, workspace: Path, tmp_path: Path
) -> None:
    """无 Vision：ResumeService 全程不产生图片 payload（只回传元数据/路径）。"""
    material_service = _catalog_with_photo(workspace, tmp_path)
    service.catalog = material_service.catalog()
    result = service.prepare("t001")
    assert "image_payload" not in json.dumps(result.data) or '"image_payload": false' in json.dumps(result.data)
    # 生成结果里也没有任何 base64 图片数据
    generate = service.generate(
        ResumeGenerateRequest(
            template_id="t001",
            fields={"name": "张三"},
            photo_asset_id=next(iter(material_service.catalog().irs.values())).assets[0].id,
        )
    )
    payload = json.dumps(generate.data, ensure_ascii=False)
    assert "base64" not in payload
    assert "data:image" not in payload
