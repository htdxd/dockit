"""共享验收数据和测量失败门；PDF 内容、边界与碰撞检查在 component_qa 中。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

LINES_TOL = 0                    # 行数必须精确一致
HEIGHT_TOL_PT = 1.0              # 高度基础容差（pt）
# R3 收紧：分隔空段样式泄漏已在 emit 根修（快照跳过空段），高度门改固定总容差
# ——行级残余仅剩 PDF ink 与 Word 行框的亚 pt 提取噪声，0.25pt/行、上限 4pt。
HEIGHT_TOL_PER_LINE_PT = 0.25
HEIGHT_TOL_MAX_PT = 4.0
MEASURED_LINE_PITCH_PT = 18.0    # COM 实测行距（t109 正文）
PITCH_TOL_PT = 1.0               # 渲染实测行距与 COM 行距的容差（pt/行）


@dataclass
class QAIssue:
    check: str
    severity: str  # "error" | "warn"
    detail: str


_BULLET_CHARS = "⚫➢•·▪◦‣⁃●○■□◼◆–—-–*✦✧►▸·。：（）():;；,，、"


def _norm(s: str) -> str:
    """匹配口径：去空白 + 去渲染侧自动生成的项目符号与结构性标点。

    输入正文按原文匹配（原文中的标点保留）；剔除的符号（bullet、冒号、
    括号等）只用于归一化**渲染流与白名单**的消费比对——标签冒号（"姓名："）
    与栏目英文括注属于版式结构，不是内容缺失/多余的信号。
    """
    s = re.sub(r"\s+", "", s)
    # Wingdings 等符号字体的项目符号：Word 导出为 Unicode（➢），WPS 保留私用区码位 U+F0xx。
    s = re.sub("[-]", "", s)
    return s.translate(str.maketrans("", "", _BULLET_CHARS))


# 兼容保留（旧签名仅做片段包含；新代码用上面的顺序版）


def check_page_count(rendered_pages: int, planned_pages: int) -> list[QAIssue]:
    if rendered_pages != planned_pages:
        return [QAIssue(
            "page_count", "error",
            f"渲染页数 {rendered_pages} != 计划页数 {planned_pages}",
        )]
    return []


def _is_valid_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # NaN 防御


def check_measurement_vs_render(
    measured: list[dict],
    rendered: list[dict],
    *,
    expected_entry_ids: list[str] | None = None,
) -> list[QAIssue]:
    """测量 vs 渲染独立对照（失败门，R3 收紧）。

    - R2 Fix3：三方 ID 集合校验（缺测量/缺渲染/未知/重复均 error）。
    - R3 Fix1：**渲染结果有效性校验**——带 `error` 字段、缺少必需数值
      （wrapped_lines/occupied_height_pt）或数值无效（None/NaN/非数）的结果
      直接 error，不再"字段缺失就跳过比较"。
    - R2/R3 Fix2：行距门（实测行距 vs COM 常数，容差 1pt）+ 高度门。
      R3 收紧：高度门容差改为**固定总容差**（基础 1 + 行级 0.25×行数，
      上限 4pt）——分隔空段样式泄漏（8.08pt）已在 emit 根修（快照跳过空段），
      不再用按行放大 1.5pt/行 的容差掩盖累计误差（20 行/379pt 反例由此拦截）。
    """
    issues: list[QAIssue] = []
    m_ids = [m["entry_id"] for m in measured]
    r_ids = [r["entry_id"] for r in rendered]
    if len(set(m_ids)) != len(m_ids):
        issues.append(QAIssue(
            "measurement_vs_render", "error",
            f"测量表 entry_id 重复: {sorted({i for i in m_ids if m_ids.count(i) > 1})}",
        ))
    if len(set(r_ids)) != len(r_ids):
        issues.append(QAIssue(
            "measurement_vs_render", "error",
            f"渲染对照表 entry_id 重复: {sorted({i for i in r_ids if r_ids.count(i) > 1})}",
        ))
    if expected_entry_ids is not None:
        exp = set(expected_entry_ids)
        miss_m = sorted(exp - set(m_ids))
        miss_r = sorted(exp - set(r_ids))
        extra_m = sorted(set(m_ids) - exp)
        extra_r = sorted(set(r_ids) - exp)
        if miss_m:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"缺少测量结果的条目（不能默认通过）: {miss_m}",
            ))
        if miss_r:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"缺少渲染对照的条目（未做独立核验）: {miss_r}",
            ))
        if extra_m:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"测量表含未知条目: {extra_m}",
            ))
        if extra_r:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"渲染对照含未知条目: {extra_r}",
            ))
    # 测量表数值有效性（缺字段/无效值 = 无法核验，必败）
    for m in measured:
        if not _is_valid_num(m.get("wrapped_lines")) or not _is_valid_num(m.get("text_height_pt")):
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"条目 {m.get('entry_id')} 测量数值无效（wrapped_lines/text_height_pt "
                f"缺失或非数值）: lines={m.get('wrapped_lines')!r} height={m.get('text_height_pt')!r}",
            ))
    rmap = {r["entry_id"]: r for r in rendered}
    for m in measured:
        r = rmap.get(m["entry_id"])
        if r is None:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"条目 {m['entry_id']} 在渲染对照表中缺失（未做独立核验，不能判通过）",
            ))
            continue
        # R3 Fix1：渲染结果有效性——error 字段 / 缺必需数值 / 无效值 必败
        if r.get("error"):
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"条目 {m['entry_id']} 渲染提取失败: {r['error']}",
            ))
            continue
        missing_fields = [
            f for f in ("wrapped_lines", "occupied_height_pt")
            if not _is_valid_num(r.get(f))
        ]
        if missing_fields:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"条目 {m['entry_id']} 渲染对照缺必需数值字段 {missing_fields}"
                f"（lines={r.get('wrapped_lines')!r} height={r.get('occupied_height_pt')!r}）",
            ))
            continue
        if abs(r["wrapped_lines"] - m["wrapped_lines"]) > LINES_TOL:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"条目 {m['entry_id']} 行数不符：测量 {m['wrapped_lines']} vs 渲染 "
                f"{r['wrapped_lines']}（差 {r['wrapped_lines'] - m['wrapped_lines']:+d}）",
            ))
        # 行距门：渲染实测行距 vs COM 常数（校准系统差后收紧为 1pt）
        if _is_valid_num(r.get("render_pitch_pt")) and m["wrapped_lines"] > 1:
            measured_pitch = m.get("line_pitch_pt", MEASURED_LINE_PITCH_PT)
            if not _is_valid_num(measured_pitch):
                issues.append(QAIssue("measurement_vs_render", "error",
                                      f"条目 {m['entry_id']} 测量行距无效"))
                continue
            dp = r["render_pitch_pt"] - measured_pitch
            if abs(dp) > PITCH_TOL_PT:
                issues.append(QAIssue(
                    "measurement_vs_render", "error",
                    f"条目 {m['entry_id']} 渲染行距异常：实测 {r['render_pitch_pt']:.1f}pt vs "
                    f"测量 {measured_pitch:.1f}pt（差 {dp:+.1f}pt，容差 {PITCH_TOL_PT}pt）",
                ))
        # 高度门（R3 收紧）：固定总容差 = 基础 + 行级 0.25×行数，上限 4pt
        dh = r["occupied_height_pt"] - m["text_height_pt"]
        tol = min(HEIGHT_TOL_MAX_PT, HEIGHT_TOL_PT + HEIGHT_TOL_PER_LINE_PT * max(1, m["wrapped_lines"]))
        if abs(dh) > tol:
            issues.append(QAIssue(
                "measurement_vs_render", "error",
                f"条目 {m['entry_id']} 占用高度不符：测量 {m['text_height_pt']:.1f}pt vs "
                f"渲染 {r['occupied_height_pt']:.1f}pt（差 {dh:+.1f}pt，容差 {tol:.1f}pt）",
            ))
    return issues


@dataclass
class QAReport:
    checks_run: list[str] = field(default_factory=list)
    issues: list[QAIssue] = field(default_factory=list)
    measurement_error_table: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks_run": self.checks_run,
            "issues": [
                {"check": i.check, "severity": i.severity, "detail": i.detail}
                for i in self.issues
            ],
            "measurement_error_table": self.measurement_error_table,
        }


def measurement_error_rows(
    measured: list[dict], rendered: list[dict]
) -> list[dict]:
    """误差表行（对照数据供人工审阅，失败判定在 check_measurement_vs_render）。

    渲染侧结果可能带 `error`（NOT FOUND 等）或缺数值：这里**只做对照数据整理**，
    绝不因缺字段崩溃（崩溃会把「可判定的失败」退化成「工具异常」）；真伪判定
    由 check_measurement_vs_render 的有效性层负责。
    """
    rows = []
    rmap = {r["entry_id"]: r for r in rendered}
    for m in measured:
        r = rmap.get(m["entry_id"])
        if r is None:
            rows.append({"entry_id": m["entry_id"], "error": "渲染对照缺失"})
            continue
        lines_m = m.get("wrapped_lines")
        lines_r = r.get("wrapped_lines")
        height_m = m.get("text_height_pt")
        height_r = r.get("occupied_height_pt")
        row: dict = {
            "entry_id": m["entry_id"],
            "first_top_measured_pt": m.get("first_line_top_pt"),
            "first_top_rendered_pt": r.get("first_line_top_pt"),
            "first_top_delta_pt": (
                round(r["first_line_top_pt"] - m["first_line_top_pt"], 2)
                if r.get("first_line_top_pt") is not None
                and m.get("first_line_top_pt") is not None
                else None
            ),
            "lines_measured": lines_m,
            "lines_rendered": lines_r,
            "lines_match": (
                lines_m is not None and lines_r is not None and lines_m == lines_r
            ),
            "render_pitch_pt": r.get("render_pitch_pt"),
            "measured_pitch_pt": m.get("line_pitch_pt"),
            "render_font_sizes_pt": r.get("render_font_sizes_pt"),
            "height_measured_pt": height_m,
            "height_rendered_pt": height_r,
            "height_delta_pt": (
                round(height_r - height_m, 2)
                if isinstance(height_m, (int, float)) and isinstance(height_r, (int, float))
                else None
            ),
        }
        if r.get("error"):
            row["error"] = r["error"]
        rows.append(row)
    return rows
