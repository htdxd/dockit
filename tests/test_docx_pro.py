"""Integration tests for the docx_pro skill: end-to-end generation + quality gate.

Covers:
- build_docx standard route (cover + CJK body + table + formula) with a real
  python-docx generation, then postcheck passes with zero errors.
- TOC injection (updateFields + TOC field present).
- fill_form (SDT + protection).
- apply_template (template preserved + content injected).
- Vision routing: the runtime injects the [CAPABILITIES] banner and non-vision
  models must not claim visual verification.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "skill_toolbox"
    / "skill_defs"
    / "docx_pro"
    / "scripts"
)


def _run_script(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=300,
        check=False,
    )


def _postcheck(docx: Path) -> list[dict]:
    completed = _run_script("postcheck_docx.py", str(docx), "--json", cwd=docx.parent)
    assert completed.returncode in (0, 2), completed.stderr
    return json.loads(completed.stdout)


def _errors(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["passed"] and r["severity"] == "error"]


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    (tmp_path / "artifacts").mkdir()
    return tmp_path


STANDARD_SPEC = {
    "title": "智能文档生成平台研究报告",
    "subtitle": "基于多技能融合的 DOCX 生成方案",
    "author": "DocKit 团队",
    "date": "2026-08-05",
    "complexity": "standard",
    "scene": "corporate",
    "cover": {"recipe": "R1", "metaLines": ["DocKit · AI 文档产物工具箱", "2026"]},
    "summary": "本报告评估将多套 DOCX 生成技能融合为统一引擎的可行性。",
    "sections": [
        {"heading": "一、背景", "level": 1, "paragraphs": [
            {"text": "随着办公自动化需求增长，可编辑、跨引擎兼容的文档生成成为核心能力。", "style": "body"},
        ]},
        {"heading": "1.1 技术路线", "level": 2, "paragraphs": [
            {"text": "采用 python-docx 作为主引擎，OfficeCLI 作为可选增强。", "style": "body"},
        ]},
    ],
    "tables": [
        {"caption": "表 1 技能来源对比", "headers": ["来源", "许可证", "可商用"],
         "rows": [["Z.AI 插件", "Proprietary", "否"], ["MiniMax", "MIT", "是"]],
         "widths_pct": [33, 33, 34]},
    ],
    "formulas": [{"latex": "E = mc^2", "caption": "质能方程"}],
}


def _write_spec(workspace: Path, spec: dict, name: str = "spec.json") -> Path:
    path = workspace / "work" / name
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def test_build_docx_standard_pipeline(workspace: Path) -> None:
    spec = _write_spec(workspace, STANDARD_SPEC)
    output = workspace / "artifacts" / "report.docx"

    result = _run_script("build_docx.py", str(spec), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr
    assert output.is_file()

    toc = _run_script("inject_toc.py", str(output), str(output), cwd=workspace)
    assert toc.returncode == 0, toc.stderr

    results = _postcheck(output)
    assert _errors(results) == []

    # TOC field + updateFields present.
    with zipfile.ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        settings = archive.read("word/settings.xml").decode("utf-8")
        footer = next(archive.read(n).decode("utf-8") for n in archive.namelist() if n.startswith("word/footer"))
    assert "TOC" in document
    assert "updateFields" in settings
    # Body footer carries the live PAGE field with the explicit arabic switch.
    assert "PAGE" in footer and "arabic" in footer
    # TOC 插在封面分节符之后、正文之前（回归：曾追加到文档末尾，目录跑最后一页）。
    toc_pos = document.find("TOC")
    sectpr = document.find("<w:sectPr")
    first_outline0 = document.find('w:outlineLvl w:val="0"')
    assert toc_pos > sectpr > 0
    assert first_outline0 == -1 or toc_pos < first_outline0
    # TOC 之后紧跟分页符（独占一页）。
    assert "type=\"page\"" in document[toc_pos : toc_pos + 3000]


def test_build_docx_embeds_image_from_asset(workspace: Path) -> None:
    """带图片的 spec 能成功生成（回归：python-docx 1.2 的 section 尺寸相减会
    退化成裸 int，usable.mm 曾崩溃，导致任何插图文档生成失败）。"""
    from PIL import Image as PILImage

    img = workspace / "work" / "fig.png"
    PILImage.new("RGB", (640, 400), (200, 120, 40)).save(img)
    spec = _write_spec(
        workspace,
        {
            "title": "带图报告",
            "sections": [{"heading": "一、插图", "level": 1}],
            "blocks": [
                {"type": "heading", "level": 1, "text": "报告"},
                {"type": "paragraph", "text": "正文段。"},
                {"type": "image", "source": str(img), "caption": "图 1 示例"},
            ],
        },
    )
    output = workspace / "artifacts" / "with-img.docx"
    result = _run_script("build_docx.py", str(spec), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr
    assert output.is_file()
    with zipfile.ZipFile(output) as archive:
        assert any(n.startswith("word/media/") for n in archive.namelist())
        document = archive.read("word/document.xml").decode("utf-8")
    assert "<w:drawing>" in document


def test_image_overflow_check_units_and_clamp(workspace: Path) -> None:
    """回归：postcheck 的 image-overflow 曾把 extent(EMU) 和页宽(twips) 直接比
    （1 twip=635 EMU），任何图片都误报超宽；且显式 width_mm 超可用宽应被自动
    钳制，postcheck 必须全绿。"""
    from PIL import Image as PILImage

    img = workspace / "work" / "big.png"
    PILImage.new("RGB", (4000, 2000), (30, 80, 200)).save(img)
    spec = _write_spec(
        workspace,
        {
            "title": "钳制回归",
            "complexity": "standard",
            "sections": [{"heading": "一、图", "level": 1}],
            "blocks": [
                {"type": "heading", "level": 1, "text": "报告"},
                {"type": "image", "source": str(img), "width_mm": 300, "caption": "图 1"},
                {"type": "image", "source": str(img), "width_mm": 90, "caption": "图 2"},
            ],
        },
    )
    output = workspace / "artifacts" / "clamp.docx"
    result = _run_script("build_docx.py", str(spec), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr
    results = _postcheck(output)
    iof = next(c for c in results if c["name"] == "image-overflow")
    assert iof["passed"], iof["message"]
    assert _errors(results) == []


def test_image_paragraph_allows_rendered_height(workspace: Path) -> None:
    """图片段落不能继承正文 exact 行距，否则 Word/WPS 会裁剪图片。"""
    from PIL import Image as PILImage

    img = workspace / "work" / "tall.png"
    PILImage.new("RGB", (400, 900), (30, 120, 80)).save(img)
    spec = _write_spec(
        workspace,
        {
            "title": "图片布局回归",
            "complexity": "simple",
            "blocks": [{"type": "image", "source": str(img), "caption": "图 1"}],
        },
    )
    output = workspace / "artifacts" / "image-layout.docx"
    result = _run_script("build_docx.py", str(spec), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr
    results = _postcheck(output)
    layout = next(c for c in results if c["name"] == "image-layout")
    assert layout["passed"], layout["message"]
    assert _errors(results) == []


def test_spec_append_builds_spec_incrementally(workspace: Path) -> None:
    """spec_append 分小批追加 blocks 到 work/spec.json，后端合并+校验，
    最终 build_docx 能直接消费（回归：大规格单次 write 会被截断）。"""
    from skill_toolbox.policy import WorkspacePolicy
    from skill_toolbox.tools import ToolRegistry

    (workspace / "work").mkdir(exist_ok=True)
    (workspace / "artifacts").mkdir(exist_ok=True)
    from PIL import Image as PILImage

    img = workspace / "work" / "fig.png"
    PILImage.new("RGB", (640, 400), (20, 80, 140)).save(img)

    policy = WorkspacePolicy(workspace)
    reg = ToolRegistry(policy, capabilities={"vision": True})

    def append(args: dict) -> str | None:
        """同步调用 _spec_append：成功返回 None，被拒返回错误文本。"""
        try:
            reg._spec_append(ToolCall(id="s", name="spec_append", arguments=args))
            return None
        except ValueError as exc:
            return str(exc)

    assert append({"spec": "work/spec.json", "title": "增量规格", "complexity": "standard",
                   "blocks": [{"type": "heading", "text": "一、架构", "level": 1},
                              {"type": "paragraph", "text": "正文。"}]}) is None
    assert append({"spec": "work/spec.json",
                   "blocks": [{"type": "image", "source": str(img), "caption": "图 1"},
                              {"type": "table", "headers": ["模型", "Top5"], "rows": [["AlexNet", "84.6%"]]}]}) is None

    spec = json.loads((workspace / "work" / "spec.json").read_text(encoding="utf-8"))
    assert [b["type"] for b in spec["blocks"]] == ["heading", "paragraph", "image", "table"]
    assert spec["title"] == "增量规格" and spec["complexity"] == "standard"

    # 校验：超 8 个 / 未知字段 被拒且不写坏文件
    over = append({"spec": "work/spec.json",
                   "blocks": [{"type": "paragraph", "text": str(i)} for i in range(9)]})
    assert over is not None and "最多 8 个" in over
    bad = append({"spec": "work/spec.json",
                  "blocks": [{"type": "heading", "text": "x", "foo": "y"}]})
    assert bad is not None and "未知字段" in bad

    # image source 存在性预检：自造路径当场拒绝并给相近候选（回归：曾拖到
    # build_docx 才 FileNotFoundError，然后烧 9 步修路径）。
    (workspace / "work" / "assets").mkdir(exist_ok=True)
    (workspace / "work" / "assets" / "abcd1234-deadbeef-missing.png").write_bytes(b"x")
    bogus = append({"spec": "work/spec.json",
                    "blocks": [{"type": "image", "source": "work/deadbeef-missing.png", "caption": "自造"}]})
    assert bogus is not None and "image source 不存在" in bogus and "abcd1234" in bogus

    # 生成端到端可用
    output = workspace / "artifacts" / "incr.docx"
    result = _run_script("build_docx.py", str(workspace / "work" / "spec.json"), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as archive:
        assert any(n.startswith("word/media/") for n in archive.namelist())
        assert "<w:drawing>" in archive.read("word/document.xml").decode("utf-8")


def test_fill_form_creates_sdt_and_protection(workspace: Path) -> None:
    form = {
        "title": "员工入职信息表",
        "font": "Microsoft YaHei",
        "protection": "forms",
        "fields": [
            {"type": "text", "label": "姓名", "alias": "Full Name", "tag": "full_name"},
            {"type": "date", "label": "入职日期", "alias": "Start Date", "tag": "start_date"},
            {"type": "checkbox", "label": "同意条款", "name": "agree_terms", "checked": False},
        ],
    }
    form_path = workspace / "work" / "form.json"
    form_path.write_text(json.dumps(form, ensure_ascii=False), encoding="utf-8")
    output = workspace / "artifacts" / "intake.docx"

    result = _run_script("fill_form.py", str(form_path), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr

    with zipfile.ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        settings = archive.read("word/settings.xml").decode("utf-8")
    assert "<w:sdt>" in document
    assert "w:checkBox" in document
    assert 'w:edit="forms"' in settings


def test_apply_template_preserves_sections(workspace: Path) -> None:
    base = workspace / "artifacts" / "base.docx"
    _run_script("build_docx.py", str(_write_spec(workspace, STANDARD_SPEC)), str(base), cwd=workspace)
    spec = workspace / "work" / "template_spec.json"
    spec.write_text(json.dumps({
        "title": "套用模板测试",
        "sections": [{"heading": "第一章 总则", "level": 1,
                      "paragraphs": [{"text": "模板套用测试正文内容。"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    output = workspace / "artifacts" / "templated.docx"

    result = _run_script("apply_template.py", str(spec), str(base), str(output), cwd=workspace)
    assert result.returncode == 0, result.stderr
    assert output.is_file()
    # Template file untouched.
    assert base.is_file()


# --- full agent-loop e2e: docx_pro through the runtime with a scripted model ---

SPEC_JSON = json.dumps(STANDARD_SPEC, ensure_ascii=False)


def _empty_content_plan(mode: str) -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "task_type": "docx",
            "mode": mode,
            "selections": [],
            "exclusions": [],
            "questions_asked": False,
        }
    )


@pytest.mark.asyncio
async def test_runtime_docx_pro_full_pipeline(tmp_path: Path) -> None:
    """A scripted model walks the docx_pro workflow (write spec → build →
    inject_toc → postcheck → finish) through the real runtime + skill scripts.
    The system prompt must carry the [CAPABILITIES] banner with vision: true."""
    captured: dict[str, str] = {}

    class _ProbeProvider(ScriptedProvider):
        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            captured["prompt"] = system_prompt
            return await super().complete(system_prompt, messages, tools)

    provider = _ProbeProvider([
        AssistantTurn(tool_calls=[ToolCall(id="plan", name="write", arguments={
            "path": "work/plans/content-plan.json",
            "content": _empty_content_plan("vision")})]),
        AssistantTurn(tool_calls=[ToolCall(id="w", name="write", arguments={
            "path": "work/spec.json", "content": SPEC_JSON})]),
        AssistantTurn(tool_calls=[ToolCall(id="b", name="exec_cmd", arguments={
            "action": "build_docx",
            "args": {"source": "work/spec.json", "output": "artifacts/report.docx"}})]),
        AssistantTurn(tool_calls=[ToolCall(id="t", name="exec_cmd", arguments={
            "action": "inject_toc",
            "args": {"source": "artifacts/report.docx", "output": "artifacts/report.docx"}})]),
        AssistantTurn(tool_calls=[ToolCall(id="p", name="exec_cmd", arguments={
            "action": "postcheck_docx",
            "args": {"source": "artifacts/report.docx"}})]),
        AssistantTurn(tool_calls=[ToolCall(id="q", name="write", arguments={
            "path": "work/qa/mechanical.json",
            "content": '{"mechanical": "passed", "visual": "passed", "used_assets": [], "skipped_assets": []}'})]),
        AssistantTurn(tool_calls=[ToolCall(id="f", name="finish_task", arguments={
            "artifacts": ["artifacts/report.docx"]})]),
    ])
    runtime = AgentRuntime(provider=provider, emit=lambda _event: None)

    result = await runtime.run(TaskRequest(
        skill_id="docx_pro",
        user_prompt="生成标准报告",
        output_dir=tmp_path,
        capabilities={"vision": True, "tool_calling": True},
    ))

    assert result.status == "completed", result.error
    assert result.artifacts[0].exists()
    assert "[CAPABILITIES]" in captured["prompt"]
    assert "vision: true" in captured["prompt"]


@pytest.mark.asyncio
async def test_runtime_docx_pro_vision_false_banner(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class _ProbeProvider(ScriptedProvider):
        async def complete(self, system_prompt, messages, tools):  # type: ignore[no-untyped-def]
            captured["prompt"] = system_prompt
            return await super().complete(system_prompt, messages, tools)

    provider = _ProbeProvider([
        AssistantTurn(tool_calls=[ToolCall(id="plan", name="write", arguments={
            "path": "work/plans/content-plan.json",
            "content": _empty_content_plan("conservative")})]),
        AssistantTurn(tool_calls=[ToolCall(id="w", name="write", arguments={
            "path": "work/spec.json", "content": SPEC_JSON})]),
        AssistantTurn(tool_calls=[ToolCall(id="b", name="exec_cmd", arguments={
            "action": "build_docx",
            "args": {"source": "work/spec.json", "output": "artifacts/report.docx"}})]),
        AssistantTurn(tool_calls=[ToolCall(id="p", name="exec_cmd", arguments={
            "action": "postcheck_docx",
            "args": {"source": "artifacts/report.docx"}})]),
        AssistantTurn(tool_calls=[ToolCall(id="q", name="write", arguments={
            "path": "work/qa/mechanical.json",
            "content": '{"mechanical": "passed", "visual": "not_run", "used_assets": [], "skipped_assets": []}'})]),
        AssistantTurn(tool_calls=[ToolCall(id="f", name="finish_task", arguments={
            "artifacts": ["artifacts/report.docx"]})]),
    ])
    runtime = AgentRuntime(provider=provider, emit=lambda _event: None)

    result = await runtime.run(TaskRequest(
        skill_id="docx_pro",
        user_prompt="生成标准报告",
        output_dir=tmp_path,
        capabilities={"vision": False, "tool_calling": True, "json_schema": True},
    ))

    assert result.status == "completed", result.error
    assert "vision: false" in captured["prompt"]
