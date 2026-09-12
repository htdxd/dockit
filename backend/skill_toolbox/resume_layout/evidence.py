# -*- coding: utf-8 -*-
"""evidence.py — P2 证据包一键复现编排。

用法：
  python evidence.py <evidence_dir> [scenario ...]
默认场景：replay clone grow twopages

流程（原件只读，全部在副本/独立目录中进行）：
 1. 间距档案  spacing.probe_template（原件副本 + Word COM）→ <ev>/spacing_archive.json
 2. 基线渲染  原件副本 → <ev>/baseline/template.pdf + page-*.png（对照基线）
 3. 逐场景    generate.py（测量 → 布局 → 落盘 → 渲染）
 4. 汇总      report.py（QA 失效门 + 误差表 + 并排预览 + 档案 vs 渲染 + 索引）

退出码：0 = 全部场景 QA 通过且档案对照通过；1 = 有失败项（详见输出）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PKG = PROJECT_ROOT / "backend/skill_toolbox/resume_layout"
TEMPLATE = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/templates/t109/template.docx"
)
RENDER_SCRIPT = (
    PROJECT_ROOT
    / "backend/skill_toolbox/skill_defs/resume_pro/scripts/render_pages.py"
)
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

DEFAULT_SCENARIOS = ("replay", "clone", "grow", "twopages")


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=env, timeout=timeout,
    )


def render_baseline(ev: Path) -> dict:
    """原件副本 → baseline/template.pdf + page-*.png（原件只读）。"""
    base = ev / "baseline"
    base.mkdir(parents=True, exist_ok=True)
    host = base / "template.docx"
    shutil.copy(TEMPLATE, host)
    rc = _run(
        [sys.executable, str(RENDER_SCRIPT), str(host), str(base)],
        cwd=base, timeout=300,
    )
    if rc.returncode != 0:
        raise RuntimeError(f"基线渲染失败：{(rc.stderr or rc.stdout)[-400:]}")
    return json.loads(rc.stdout.strip().splitlines()[-1])


def build_archive(ev: Path) -> dict:
    from skill_toolbox.resume_layout import spacing as S

    arch = S.probe_template(TEMPLATE)
    S.save_archive(arch, ev / "spacing_archive.json")
    return arch.to_dict()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    ev = Path(sys.argv[1]).resolve()
    ev.mkdir(parents=True, exist_ok=True)
    scenarios = tuple(sys.argv[2:]) or DEFAULT_SCENARIOS

    out: dict = {"evidence_dir": str(ev)}
    out["baseline"] = render_baseline(ev)
    arch = build_archive(ev)
    out["archive"] = {
        "template_sha256": arch["template_sha256"],
        "sections": arch["sections"],
        "pairs": arch["pairs"],
    }

    for name in scenarios:
        rc = _run(
            [sys.executable, str(PKG / "generate.py"), str(ev), name],
            cwd=ev, timeout=900,
        )
        if rc.returncode != 0:
            out[name] = {"ok": False, "stderr": (rc.stderr or rc.stdout)[-800:]}
            continue
        res = json.loads(rc.stdout)
        out[name] = {
            "ok": res.get("ok"),
            "plan_pages": res.get("plan_pages"),
            "rendered_pages": res.get("rendered_pages"),
        }

    rc = _run(
        [sys.executable, str(PKG / "report.py"), str(ev)], cwd=ev, timeout=900
    )
    if rc.returncode != 0:
        print(json.dumps({"ok": False, "stage": "report", "stderr": rc.stderr[-800:]},
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    summary = json.loads(rc.stdout)
    out["qa"] = summary["scenarios"]
    out["archive_vs_render"] = summary["archive_vs_render"]

    failed = [k for k, v in summary["scenarios"].items() if not v["passed"]]
    archive_ok = (summary["archive_vs_render"] or {}).get("passed", True)
    out["ok"] = not failed and archive_ok
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
