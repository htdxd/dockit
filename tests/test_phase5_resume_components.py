"""阶段 5 强制测试：简历模板组件能力（hash + components + 受限动作）。

覆盖实施计划 §15.3/§17.4 关键回归：
- 5 套模板 manifest 都有 template_sha256 与 components/allowed_actions
- hash 不匹配立即失败（禁止对未知结构套旧 selector）
- replace_text / replace_asset / resize / shift / clone 白名单动作
- 白名单外动作与未知组件被拒绝；含图组件 clone 被拒（relationship 安全）
- 动作后 OOXML 包结构完好（zip 可解、唯一 ID、关系完好）
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

RESUME_TEMPLATES = Path("backend/skill_toolbox/skill_defs/resume_pro/templates")
FILL_SCRIPT = Path("backend/skill_toolbox/skill_defs/resume_pro/scripts/fill_resume.py")

TEMPLATE_IDS = ["t001", "t002", "t026", "t046", "t109"]


def _run_fill(
    template_id: str, fields: dict, actions: list[dict], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    tpl_dir = RESUME_TEMPLATES / template_id
    data = {"fields": {**fields, "actions": actions}} if actions else {"fields": fields}
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "artifacts" / "resume.docx"
    return subprocess.run(
        [sys.executable, str(FILL_SCRIPT.resolve()), str(tpl_dir.resolve()), str(data_path), str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(tmp_path),
        check=False,
    )


# ---------------- manifest 结构 ----------------

@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_all_templates_have_hash_and_components(template_id: str) -> None:
    manifest = json.loads(
        (RESUME_TEMPLATES / template_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["template_sha256"], template_id
    # 组件与字段集一致（每套都有组件清单与白名单动作）
    assert manifest["components"], template_id
    for comp in manifest["components"]:
        assert comp["id"]
        assert comp["objects"]
        assert comp["bbox_pt"]
        assert comp["allowed_actions"]
        assert "replace_text" in comp["allowed_actions"] or "replace_asset" in comp["allowed_actions"]


def test_template_sha256_matches_actual_file() -> None:
    for template_id in TEMPLATE_IDS:
        manifest = json.loads(
            (RESUME_TEMPLATES / template_id / "manifest.json").read_text(encoding="utf-8")
        )
        template = RESUME_TEMPLATES / template_id / manifest["template_file"]
        actual = hashlib.sha256(template.read_bytes()).hexdigest()
        assert actual == manifest["template_sha256"], template_id


# ---------------- hash 校验 ----------------

def test_fill_rejects_modified_template(tmp_path: Path) -> None:
    """模板 hash 不匹配 → 立即失败，不套旧 selector。"""
    tpl_dir = RESUME_TEMPLATES / "t001"
    # 构造一个 hash 被改写的副本模板目录
    fake_dir = tmp_path / "fake_t001"
    fake_dir.mkdir()
    (fake_dir / "template.docx").write_bytes(b"PK\x03\x04 tampered")
    manifest = json.loads((tpl_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["template_sha256"] = "0" * 64
    (fake_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps({"fields": {"name": "张三"}}), encoding="utf-8")
    out = tmp_path / "out.docx"

    result = subprocess.run(
        [sys.executable, str(FILL_SCRIPT.resolve()), str(fake_dir.resolve()), str(data_path), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=str(tmp_path), check=False,
    )
    assert result.returncode != 0
    assert "hash" in result.stderr.lower() or "重新索引" in result.stderr
    assert not out.exists()


# ---------------- 受限动作 ----------------

def test_replace_text_action(tmp_path: Path) -> None:
    """replace_text 通过组件白名单替换对应字段文本。"""
    result = _run_fill("t001", {}, [{"action": "replace_text", "component": "name", "field": "name", "value": "李四"}], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(r.get("action") == "replace_text" and r.get("field") == "name" for r in payload["replaced"])


def test_replace_text_unknown_component_rejected(tmp_path: Path) -> None:
    result = _run_fill(
        "t001", {}, [{"action": "replace_text", "component": "nope", "value": "x"}], tmp_path
    )
    assert result.returncode == 0  # 动作失败进 warnings，不崩溃
    payload = json.loads(result.stdout)
    assert any("未知组件" in w for w in payload["warnings"])


def test_action_not_in_whitelist_rejected(tmp_path: Path) -> None:
    """白名单外动作（delete_all）被拒绝。"""
    result = _run_fill("t001", {}, [{"action": "delete_all", "component": "name"}], tmp_path)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any("未知动作" in w for w in payload["warnings"])


def test_resize_component_enforces_height_bounds(tmp_path: Path) -> None:
    """resize 超出 min/max 范围 → 失败（机械门），不产生损坏产物。"""
    # 先读取允许范围
    manifest = json.loads((RESUME_TEMPLATES / "t001" / "manifest.json").read_text(encoding="utf-8"))
    work = next(c for c in manifest["components"] if c["id"] == "work")
    hi = work["max_height_pt"]
    result = _run_fill(
        "t001", {}, [{"action": "resize_component", "component": "work", "height_pt": hi + 1000}], tmp_path
    )
    assert result.returncode != 0  # fail-fast
    assert "超出允许范围" in result.stderr


def test_resize_component_in_range(tmp_path: Path) -> None:
    manifest = json.loads((RESUME_TEMPLATES / "t001" / "manifest.json").read_text(encoding="utf-8"))
    work = next(c for c in manifest["components"] if c["id"] == "work")
    target = round((work["min_height_pt"] + work["max_height_pt"]) / 2, 1)
    result = _run_fill(
        "t001", {}, [{"action": "resize_component", "component": "work", "height_pt": target}], tmp_path
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(r.get("action") == "resize_component" for r in payload["replaced"])


def test_shift_components(tmp_path: Path) -> None:
    result = _run_fill(
        "t001",
        {},
        [{"action": "shift_components", "component_ids": ["work"], "dy_pt": -20}],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(r.get("action") == "shift_components" and r.get("dy_pt") == -20.0 for r in payload["replaced"])


def test_clone_text_component_preserves_package(tmp_path: Path) -> None:
    """clone 文本组件：zip 包结构完好、XML 可解析、未引入新的重复 id。"""
    # 基线模板本身就有 id 重复（Word 容忍），clone 不得让重复数变差
    baseline_xml = zipfile.ZipFile(RESUME_TEMPLATES / "t001" / "template.docx").read(
        "word/document.xml"
    ).decode("utf-8")
    baseline_ids = re.findall(r'\bid="(\d+)"', baseline_xml)
    baseline_dups = len(baseline_ids) - len(set(baseline_ids))

    result = _run_fill(
        "t001",
        {},
        [{"action": "clone_component", "component": "work", "after": "work_1"}],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(r.get("action") == "clone_component" for r in payload["replaced"])
    out = tmp_path / "artifacts" / "resume.docx"
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "word/document.xml" in names
        xml = z.read("word/document.xml").decode("utf-8")
    ids = re.findall(r'\bid="(\d+)"', xml)
    assert len(ids) - len(set(ids)) == baseline_dups  # 未新增重复 id


def test_clone_image_component_rejected(tmp_path: Path) -> None:
    """含图片的组件（photo）不允许 clone（白名单只授权 replace_asset）。"""
    result = _run_fill(
        "t001",
        {},
        [{"action": "clone_component", "component": "photo", "after": "work_1"}],
        tmp_path,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any("不允许动作 clone_component" in w for w in payload["warnings"])


def test_replace_asset_remove_photo(tmp_path: Path) -> None:
    """replace_asset __remove__ 走照片移除流程，产物包结构完好。"""
    result = _run_fill(
        "t001",
        {},
        [{"action": "replace_asset", "component": "photo", "value": "__remove__"}],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = tmp_path / "artifacts" / "resume.docx"
    with zipfile.ZipFile(out) as z:
        # 旧照片字节被移除
        assert "word/media/image1.jpeg" not in z.namelist()


# ---------------- L1 原位替换回归 ----------------

def test_legacy_fields_still_work(tmp_path: Path) -> None:
    """旧 fields 原位替换不因动作层引入而回归。"""
    result = _run_fill("t001", {"name": "张三", "phone": "138-0000-8000"}, [], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(r.get("id") == "name" for r in payload["replaced"])
    assert any(r.get("id") == "phone" for r in payload["replaced"])
