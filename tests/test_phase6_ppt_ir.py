"""阶段 6 强制测试：PPT 共享 IR 接入（wrapper 复用 ppt-master parser）。

覆盖实施计划 §10.1/§15.3 关键回归：
- PPTX 材料经共享层解析为 DocumentIR（文本/表格/图片块 + Asset 引用）
- 图片块能定位 Asset（素材物化到 work/materials/<id>/assets/）
- 同一份富媒体材料生成 PPT 时使用的 Asset 可回溯（selections 留痕）
- ppt-master 导出路径不回归（manifest 声明 tool_calling + 长 action 超时）
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from skill_toolbox.materials import MaterialService, material_id_dir


def _make_minimal_pptx(path: Path) -> None:
    """构建最小合法 PPTX（python-pptx 可打开；含一页文本 + 一张图）。"""
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "阶段 6 测试标题"
    slide.placeholders[1].text = "- 要点一\n- 要点二"
    prs.save(path)


@pytest.mark.skipif(
    not Path("backend/skill_toolbox/skill_defs/ppt-master/scripts/source_to_md/ppt_to_md.py").is_file(),
    reason="ppt-master parser 缺失（vendored skill 未安装）",
)
def test_pptx_parsed_into_shared_ir(tmp_path: Path) -> None:
    """PPTX → DocumentIR：文本块 + 图片块 Asset 引用 + 素材物化。"""
    source = tmp_path / "src" / "deck.pptx"
    source.parent.mkdir()
    _make_minimal_pptx(source)
    ws = tmp_path / "ws"
    ws.mkdir()
    svc = MaterialService(ws)

    ir = svc.prepare_material(source)

    assert ir.source_format == "pptx"
    assert ir.blocks, "PPTX 解析未产出任何块"
    types = [b.type for b in ir.blocks]
    assert "heading" in types
    assert any("阶段 6 测试标题" in b.text for b in ir.blocks)
    # 文本来自 parser 输出（第二事实不再产生：共享 IR 是唯一解析）
    assert any("要点" in b.text for b in ir.blocks)
    # 兼容投影存在
    md = material_id_dir(ws, ir.material_id) / "content.md"
    assert md.is_file()
    assert "阶段 6 测试标题" in md.read_text(encoding="utf-8")


def test_ppt_parser_does_not_request_mineru_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    source = tmp_path / "deck.pptx"
    _make_minimal_pptx(source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = MaterialService(
        workspace,
        extra_env={"MINERU_TOKEN": "must-not-leak"},
        mineru_token="sidecar-secret",
    )
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        target = Path(command[command.index("-o") + 1])
        target.write_text("# Parsed deck", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(service, "_run_subprocess", fake_run)

    service.prepare_material(source)

    assert calls and calls[0].get("use_mineru_token") is not True


@pytest.mark.skipif(
    not Path("backend/skill_toolbox/skill_defs/ppt-master/scripts/source_to_md/ppt_to_md.py").is_file(),
    reason="ppt-master parser 缺失",
)
def test_pptx_image_asset_traceable(tmp_path: Path) -> None:
    """带图片的 PPTX：image block 的 asset_ids 可定位到物化 Asset。"""
    from pptx import Presentation
    from pptx.util import Inches

    source = tmp_path / "src" / "with_img.pptx"
    source.parent.mkdir()
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    img_path = tmp_path / "fig.png"
    img_path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    )  # 极小 PNG 头（python-pptx 只校验可读性）
    # 用 PIL 生成真实 PNG，避免 python-pptx 拒绝
    from PIL import Image

    Image.new("RGB", (60, 40), (200, 100, 50)).save(img_path)
    slide.shapes.add_picture(str(img_path), Inches(1), Inches(1))
    prs.save(source)

    ws = tmp_path / "ws"
    ws.mkdir()
    svc = MaterialService(ws)
    ir = svc.prepare_material(source)

    image_blocks = [b for b in ir.blocks if b.type == "image"]
    assert image_blocks, "PPTX 图片未解析为 image block"
    for block in image_blocks:
        assert block.asset_ids, f"image block {block.id} 无 Asset 引用"
        for asset_id in block.asset_ids:
            asset = ir.asset_by_id(asset_id)
            assert asset is not None
            assert not Path(asset.path).is_absolute()
            assert (ws / asset.path).is_file()
            assert asset.mime_type.startswith("image/")


def test_ppt_manifest_requires_tool_calling_with_timeouts() -> None:
    """ppt-master manifest 声明 tool_calling required + 长 action 超时（§12.1）。"""
    from skill_toolbox.skills import load_skill

    skill = load_skill("ppt-master")
    assert "tool_calling" in skill.required_capabilities
    assert skill.script_timeouts.get("source_to_md", 0) >= 60
    assert skill.script_timeouts.get("svg_to_pptx", 0) >= 60


def test_ppt_prompt_no_duplicate_source_to_md() -> None:
    """PPT Prompt 不要求模型另跑 source_to_md（共享 IR 已解析，§10.1）。"""
    from skill_toolbox.skills import load_skill

    prompt = load_skill("ppt-master").system_prompt
    # 统一材料协议要求先读共享 IR/ContentPlan
    assert "ContentPlan" in prompt
    assert "[MATERIALS]" in prompt or "材料" in prompt
    # 统一材料协议段（本段到下一个 "## " 标题之间）不得指导"先跑 source_to_md
    # 转换材料"（材料理解已由共享 IR 完成，§10.1）；source_to_md 只出现在
    # 后面的 Generate 路由 action 说明里。
    protocol_section = (
        prompt.split("## 统一材料协议", 1)[1].split("## ", 1)[0]
        if "## 统一材料协议" in prompt
        else ""
    )
    assert protocol_section and "source_to_md" not in protocol_section
    # Generate 路由仍可用 source_to_md action（作为 action 而非材料解析第一步）
    assert "source_to_md" in prompt


def test_pptx_package_remains_valid_after_ir(tmp_path: Path) -> None:
    """解析只读原始材料，不修改源文件；原始 PPTX 包仍完整可解。"""
    source = tmp_path / "src" / "deck.pptx"
    source.parent.mkdir()
    _make_minimal_pptx(source)
    before = source.read_bytes()
    ws = tmp_path / "ws"
    ws.mkdir()
    svc = MaterialService(ws)
    svc.prepare_material(source)
    assert source.read_bytes() == before
    with zipfile.ZipFile(source) as z:
        assert "ppt/presentation.xml" in z.namelist()
