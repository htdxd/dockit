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
    scripts: dict[str, tuple[str, tuple[str, ...]]]
    dir: Path
    required_capabilities: frozenset[str] = frozenset()
    optional_capabilities: frozenset[str] = frozenset()


def load_skill(skill_id: str) -> SkillDefinition:
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
    return SkillDefinition(
        id=manifest["id"],
        name=manifest["name"],
        description=manifest["description"],
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        max_steps=int(manifest.get("max_steps", 12)),
        initial_form=manifest.get("initial_form", {}),
        scripts=scripts,
        dir=skill_dir,
        required_capabilities=frozenset(manifest.get("required_capabilities", [])),
        optional_capabilities=frozenset(manifest.get("optional_capabilities", [])),
    )
