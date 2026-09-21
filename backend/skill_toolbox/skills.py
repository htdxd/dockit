from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    system_prompt: str
    max_steps: int
    initial_form: dict[str, Any]
    dir: Path
    required_capabilities: frozenset[str] = frozenset()
    optional_capabilities: frozenset[str] = frozenset()


RETIRED_PDF_IDS = frozenset({"pdf_docx_routing", "pdf_to_docx"})
FEATURE_RETIRED_MESSAGE = "[FEATURE_RETIRED] PDF 转 DOCX 功能已下线；PDF 仍可作为生成材料。"


def load_skill(skill_id: str) -> SkillDefinition:
    if skill_id in RETIRED_PDF_IDS:
        raise ValueError(FEATURE_RETIRED_MESSAGE)
    if skill_id != "resume_pro":
        raise ValueError("[FEATURE_RETIRED] 当前产品仅支持简历制作。")
    skill_dir = Path(__file__).parent / "skill_defs" / skill_id
    manifest_path = skill_dir / "manifest.json"
    prompt_path = skill_dir / "workflow.md"
    if not manifest_path.is_file() or not prompt_path.is_file():
        raise ValueError(f"Unknown skill: {skill_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    system_prompt = prompt_path.read_text(encoding="utf-8")
    return SkillDefinition(
        id=manifest["id"],
        name=manifest["name"],
        description=manifest["description"],
        system_prompt=system_prompt,
        max_steps=int(manifest.get("max_steps", 12)),
        initial_form=manifest.get("initial_form", {}),
        dir=skill_dir,
        required_capabilities=frozenset(manifest.get("required_capabilities", [])),
        optional_capabilities=frozenset(manifest.get("optional_capabilities", [])),
    )
