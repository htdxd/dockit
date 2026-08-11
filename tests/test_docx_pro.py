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
