"""共用的构建流程与位置约束；不负责版本和候选状态。"""
import json
import math
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.resume_layout import (
    component_body,
    component_qa,
    component_template,
    spacing,
)
from skill_toolbox.resume_layout import layout as RL
from skill_toolbox.resume_layout.profiles import TemplateProfile


@dataclass
class RenderedResume:
    plan: RL.LayoutPlan
    qa: dict
    header: dict
    docx: Path
    pdf: Path
    pages: list[Path]
    single_page_fit: dict | None


class SpacingOverrides:
    def __init__(self, archive, content):
        self.archive = archive
        self.compact = content.get('density') == 'compact'
        self.gaps = {s['key']: s['entry_gap_pt'] for s in content['sections'] if s.get('entry_gap_pt') is not None}

    def delta_after(self, previous, following, default):
        return 8.0 if self.compact else self.archive.delta_after(previous, following, default)

    def entry_gap_for(self, section, default):
        return self.gaps.get(section, 3.0 if self.compact else self.archive.entry_gap_for(section, default))


def build(scenario: dict, content: dict, output_dir: Path, *, template: Path,
          profile: TemplateProfile, run_stage: Callable[[list[str], str], None],
          layout_mode: str = 'reflow') -> RenderedResume:
    """完成一次构建；run_stage 是唯一外部执行接口，版本提交由调用方负责。"""
    work = output_dir / 'build'
    work.mkdir(parents=True, exist_ok=True)
    payload = {'sections': scenario['sections'], 'header': {
        key: value for key, value in scenario.get('header', {}).items()
        if key in {'fields', 'hidden_fields', 'custom_fields', 'hide_photo'}}}
    input_path, archive_path = work / 'measure_input.json', work / 'spacing.json'
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    run_stage([sys.executable, '-m', 'skill_toolbox.resume_layout.pipeline', str(template),
               str(input_path), str(work), str(archive_path), profile.template_id], 'MEASURE_FAILED')
    scenario.setdefault('header', {})['fit'] = json.loads((work / 'header_fit.json').read_text(encoding='utf-8'))
    rows = json.loads((work / 'measure_result.json').read_text(encoding='utf-8'))['results']
    measured = {r['entry_id']: RL.MeasureResult.from_dict(r) for r in rows}
    gaps = SpacingOverrides(spacing.load_archive(archive_path), content)
    adjust = geometry_adjust(content) if layout_mode == 'reflow' else None

    def plan_for(geometry):
        return component_template.plan_scenario(scenario, measured, template_id=profile.template_id,
            geometry=geometry, spacing=gaps, min_visible_gap_pt=8.0, entry_adjust=adjust)

    try:
        plan = plan_for(profile.geometry)
    except RL.LayoutUnsatisfiable as exc:
        raise ToolError('LAYOUT_UNSATISFIABLE', str(exc), retryable=True,
                        suggestion='精简超长条目或拆分为多条；不要删内容绕过检查。') from None
    if layout_mode == 'local':
        plan = apply_local_geometry(plan, content, profile.geometry)
    else:
        validate_plan_geometry(plan, profile.geometry)
    fit = None
    if layout_mode == 'reflow' and plan.pages > 1:
        unpaged = plan_for(replace(profile.geometry, page_bottom_pt=1_000_000))
        bottom = max(e.anchor_y_pt + e.body_h_pt for s in unpaged.sections for e in s.entries)
        reduction = max(0, bottom - profile.geometry.page_bottom_pt)
        pitch = min(r['line_pitch_pt'] for r in rows)
        fit = {'required_reduction_pt': round(reduction, 1),
               'approx_lines_to_save': math.ceil(reduction / pitch), 'reference_line_pitch_pt': pitch}
    docx, render_dir = output_dir / 'resume.docx', output_dir / 'render'
    rendered = component_body.emit_scenario(scenario, plan, docx, template=template, template_id=profile.template_id)
    render_dir.mkdir(parents=True, exist_ok=True)
    render_script = Path(__file__).resolve().parents[1] / 'skill_defs/resume_pro/scripts/render_pages.py'
    run_stage([sys.executable, str(render_script), str(docx), str(render_dir)], 'RENDER_FAILED')
    parts = json.loads((work / 'component_measurements.json').read_text(encoding='utf-8'))['parts']
    parts = [{**r, 'entry_id': r['measurement_id']} for r in parts]
    pdf = render_dir / 'resume.pdf'
    report = component_qa.check_components(pdf, scenario, plan.to_dict(), rendered, parts, rendered['header']['fields'])
    for name, data in [('layout_plan', plan.to_dict()), ('qa_report', report)]:
        (output_dir / f'{name}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.rmtree(work, ignore_errors=True)
    return RenderedResume(plan, report, rendered['header'], docx, pdf, sorted(render_dir.glob('page-*.png')), fit)


def geometry_adjust(content: dict[str, Any]) -> dict[str, dict[str, float]]:
    """把内容里的几何覆盖整理成 layout 的 `entry_adjust`（仅 reflow 用）。

    reflow 语义下，几何约束必须**参与完整 flow 排版**：下移/改高后，其后的
    条目、后续栏目与分页一起重排（P2-R2 修复——旧实现只在布局结果上平移
    本栏条目，后续栏目不动，留下交叠）。
    """
    adjust: dict[str, dict[str, float]] = {}
    for sec in content.get("sections", []):
        for entry in sec.get("entries", []):
            per: dict[str, float] = {}
            if entry.get("dy_pt"):
                per["dy_pt"] = float(entry["dy_pt"])
            if entry.get("height_pt") is not None:
                per["height_pt"] = float(entry["height_pt"])
            elif entry.get("height_delta_pt"):
                per["height_delta_pt"] = float(entry["height_delta_pt"])
            if per:
                adjust[str(entry["id"])] = per
    return adjust


def apply_local_geometry(
    plan: Any, content: dict[str, Any], geometry: Any
) -> Any:
    """`local` 模式：只动目标条目，其它组件保持规划位置。

    目标与相邻条目/栏目碰撞、或越出页容量 → 整批失败（不偷偷推开别人）。
    """
    import copy

    plan = copy.deepcopy(plan)
    for sec_content in content.get("sections", []):
        sec_plan = next(
            (s for s in plan.sections if s.section_id == sec_content["key"]), None
        )
        if sec_plan is None:
            continue
        for entry_content in sec_content.get("entries", []):
            dy = float(entry_content.get("dy_pt", 0.0) or 0.0)
            delta_h = float(entry_content.get("height_delta_pt", 0.0) or 0.0)
            height_abs = entry_content.get("height_pt")
            if not dy and not delta_h and height_abs is None:
                continue
            plan_entry = next(
                (e for e in sec_plan.entries if e.instance_id == entry_content["id"]),
                None,
            )
            if plan_entry is None:
                raise ToolError("TARGET_NOT_FOUND", f"条目不存在: {entry_content['id']}")
            offset = getattr(plan_entry, "text_top_offset_pt", None)
            min_h = (geometry.text_top_pt if offset is None else offset) + plan_entry.text_height_pt + 0.5
            if height_abs is not None:
                target_h = float(height_abs)
                if target_h < min_h:
                    raise ToolError(
                        "BOUNDS_VIOLATION",
                        f"height_pt={target_h} 小于文字实际需要的高度 "
                        f"{min_h:.1f}pt（会裁切文字）",
                    )
                delta_h = target_h - plan_entry.body_h_pt
            if delta_h:
                new_h = plan_entry.body_h_pt + delta_h
                if new_h < min_h:
                    raise ToolError(
                        "BOUNDS_VIOLATION",
                        f"{plan_entry.instance_id} 调整后高度 {new_h:.1f}pt "
                        f"小于文字高度 {min_h:.1f}pt（会裁切文字）",
                    )
                plan_entry.body_h_pt = new_h
            if dy:
                plan_entry.anchor_y_pt += dy
            if entry_content.get("id") == sec_content["entries"][0]["id"] and dy:
                sec_plan.anchor_y_pt += dy
    validate_plan_geometry(plan, geometry)
    return plan


def validate_plan_geometry(plan: Any, geometry: Any) -> None:
    """终检：条目不越界、同栏相邻文字不相撞、**跨栏目**文字不相撞。

    跨页时页内坐标重新起算（同一栏目可跨页）。
    """
    for sec_plan in plan.sections:
        prev_text_bottom: float | None = None
        prev_page: int | None = None
        for entry in sec_plan.entries:
            if entry.page_index != prev_page:
                prev_text_bottom = None
                prev_page = entry.page_index
            if entry.anchor_y_pt < 0:
                raise ToolError(
                    "BOUNDS_VIOLATION",
                    f"{entry.instance_id} 被移动到页面上边界之外 "
                    f"(anchor_y={entry.anchor_y_pt:.1f})",
                )
            offset = getattr(entry, "text_top_offset_pt", None)
            text_top = entry.anchor_y_pt + (geometry.text_top_pt if offset is None else offset)
            if (
                prev_text_bottom is not None
                and text_top - prev_text_bottom < RL.MIN_ENTRY_GAP_PT
            ):
                raise ToolError(
                    "COLLISION",
                    f"{entry.instance_id} 与上一条文字重叠 "
                    f"(间距 {text_top - prev_text_bottom:.1f}pt < "
                    f"{RL.MIN_ENTRY_GAP_PT}pt)",
                )
            bottom = entry.anchor_y_pt + entry.body_h_pt
            if bottom > geometry.page_bottom_pt + 1e-6:
                raise ToolError(
                    "BOUNDS_VIOLATION",
                    f"{entry.instance_id} 移动后越界：底 {bottom:.1f}pt > "
                    f"{geometry.page_bottom_pt}pt",
                )
            prev_text_bottom = text_top + entry.text_height_pt
    try:
        for region in {s.region for s in plan.sections}:
            RL.check_section_flow([s for s in plan.sections if s.region == region], geometry=geometry)
    except RL.LayoutUnsatisfiable as exc:
        raise ToolError("COLLISION", str(exc)) from None
