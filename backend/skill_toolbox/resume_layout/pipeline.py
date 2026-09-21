"""六模板共用的正文/头部测量和输出入口。"""
import json
import sys
from pathlib import Path

from skill_toolbox.resume_layout.component_body import emit_scenario, measure_scenario
from skill_toolbox.resume_layout.component_template import ensure_archive

__all__ = ["emit_scenario", "measure_scenario"]


def main() -> None:
    template, spec, work, archive = map(Path, sys.argv[1:5])
    template_id = sys.argv[5] if len(sys.argv) > 5 else "t109"
    if str(spec) != "-":
        measure_scenario(json.loads(spec.read_text(encoding="utf-8")), work,
                         template=template, template_id=template_id)
    ensure_archive(template, archive, template_id)


if __name__ == "__main__":
    main()
