from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    system_prompt: str
    max_steps: int
    initial_form: dict[str, Any]
    scripts: dict[str, tuple[str, tuple[str, ...]]]
    dir: Path
    required_capabilities: frozenset[str] = frozenset()
    optional_capabilities: frozenset[str] = frozenset()
    # 每个 action 的可选长超时（秒）。MinerU 转换、Office 渲染等脚本内部
    # 允许几分钟到 30 分钟，Runtime 外层必须按 action 覆盖默认短超时，
    # 禁止出现外层 90s 提前终止内部 1800s 的 MinerU（见实施计划 §9.5）。
    script_timeouts: dict[str, float] = dataclasses.field(default_factory=dict)
    # Deterministic actions whose successful execution can satisfy the
    # mechanical delivery gate. Agent-authored QA JSON is not trusted alone.
    quality_actions: frozenset[str] = frozenset()
    # 受控工具模式："legacy"（默认，旧 ToolRegistry + 通用底层工具）或
    # "domain"（领域级 LLM tools，普通任务不暴露 write/edit/exec_cmd/
    # spec_append——实施计划 §3.1/§8）。由 manifest 的 tool_mode 声明。
    tool_mode: Literal["domain", "legacy"] = "legacy"


RETIRED_PDF_IDS = frozenset({"pdf_docx_routing", "pdf_to_docx"})
FEATURE_RETIRED_MESSAGE = "[FEATURE_RETIRED] PDF 转 DOCX 功能已下线；PDF 仍可作为生成材料。"


def load_skill(skill_id: str) -> SkillDefinition:
    if skill_id in RETIRED_PDF_IDS:
        raise ValueError(FEATURE_RETIRED_MESSAGE)
    if skill_id != "resume_pro":
        raise ValueError("[FEATURE_RETIRED] 当前产品仅支持简历制作。")
    skill_dir = Path(__file__).parent / "skill_defs" / skill_id
    manifest_path = skill_dir / "manifest.json"
    prompt_path = skill_dir / "prompt.md"
    if not manifest_path.is_file() or not prompt_path.is_file():
        raise ValueError(f"Unknown skill: {skill_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scripts = {
        name: (spec["entry"], tuple(spec.get("argv", [])))
        for name, spec in manifest.get("scripts", {}).items()
    }
    script_timeouts = {
        name: float(spec["timeout_seconds"])
        for name, spec in manifest.get("scripts", {}).items()
        if spec.get("timeout_seconds")
    }
    system_prompt = prompt_path.read_text(encoding="utf-8")
    raw_tool_mode = str(manifest.get("tool_mode", "legacy"))
    if raw_tool_mode not in {"domain", "legacy"}:
        raise ValueError(
            f"Skill {manifest['id']} manifest.tool_mode 必须是 domain 或 legacy，"
            f"实际为 {raw_tool_mode!r}"
        )
    return SkillDefinition(
        id=manifest["id"],
        name=manifest["name"],
        description=manifest["description"],
        system_prompt=system_prompt,
        max_steps=int(manifest.get("max_steps", 12)),
        initial_form=manifest.get("initial_form", {}),
        scripts=scripts,
        dir=skill_dir,
        required_capabilities=frozenset(manifest.get("required_capabilities", [])),
        optional_capabilities=frozenset(manifest.get("optional_capabilities", [])),
        script_timeouts=script_timeouts,
        quality_actions=frozenset(manifest.get("quality_actions", [])),
        tool_mode=raw_tool_mode,  # type: ignore[arg-type]
    )
