# -*- coding: utf-8 -*-
"""report.py — P1 证据包 QA 汇总（修订版：独立核验 + 失败门）。

对每个场景产出 qa_report.json：
- content_completeness：完整文本顺序游标匹配（截断/漏内容/顺序错乱均 error）
- page_count / within_page / no_overlap / title_uniqueness（同前）
- measurement_vs_render：**渲染侧独立提取**每条目 wrap 行数与占用高度，
  对照 COM 测量值；行数差>0 或高度差>1pt → error（QA 不通过）
  ——篡改测量值（如 lines=999、height=17982）必然触发失败，无法自证通过。

渲染侧独立提取口径：
- 行数：条目首行顶 y0 到「下一条目首行顶 − 条目间隙」区间内按 18pt 行距带计数
- 占用高度：行数 × 18.0pt（渲染实测行距；与 COM 测量 text_height 同口径比较）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from skill_toolbox.resume_layout import qa  # noqa: E402

FOOTER_TOP = 833.6
PAGE_TOP = 193.35
TITLES = ("教育背景", "实习经历", "校园经历", "技能证书", "自我评价", "项目经历")
# 段内连续行判定上限（pt）：段内行距实测 18.0–18.05；下一条目首行至少低
# 19.04pt（档案条目间距 5.2 + ink 13.84）或 22.29pt（默认 8.45 + 13.84），
# 因此 18.6 能把「本段换行」与「下一条目首行」分开。
LINE_PITCH_CONTINUATION_PT = 18.6
SECTION_TITLE_PREFIX = {
    "education": "教育背景",
    "internship": "实习经历",
    "campus": "校园经历",
    "skills": "技能证书",
    "summary": "自我评价",
}
RENDER_LINE_PITCH = 18.0     # 渲染实测行距（P0 基线）
ENTRY_GAP_RENDER = 26.4      # 同栏相邻条目首行最小间隔（29.2+3.85-6.05 渲染域差）
# 母版个人信息区的模板固定文字（额外内容检查白名单；值为模板示例，
# P1 探针不改 header 字段——替换 header 属 P2 内容模型工作）
_HEADER_WHITELIST = [
    "个人信息", "PersonalInfo",
    "姓名", "简历模板资源网", "民族", "汉", "电话", "13500135000",
    "邮箱", "jianlimoban-ziyuan.com", "住址", "北京市东城区",
    "出生年月", "1996.05", "身高", "177cm", "政治面貌", "中共党员",
    "毕业院校", "简历模板资源网大学", "学历", "本科",
]


def rendered_words(pdf_path: Path) -> list[dict]:
    doc = fitz.open(str(pdf_path))
    rows = []
    for pi in range(len(doc)):
        for w in doc[pi].get_text("words"):
            rows.append({
                "page": pi, "y0": w[1], "y1": w[3], "x0": w[0], "x1": w[2], "text": w[4],
            })
    return rows


def overlap_pairs(rows: list[dict]) -> list[dict]:
    bad = []
    for page in {r["page"] for r in rows}:
        pr = sorted([r for r in rows if r["page"] == page], key=lambda r: (r["y0"], r["x0"]))
        for i, a in enumerate(pr):
            for b in pr[i + 1:]:
                if b["y0"] >= a["y1"] - 0.5:
                    break
                if a["x1"] > b["x0"] + 0.5 and b["x1"] > a["x0"] + 0.5:
                    bad.append({
                        "page": page + 1,
                        "a": f"{a['text'][:20]}@y{a['y0']:.0f}",
                        "b": f"{b['text'][:20]}@y{b['y0']:.0f}",
                    })
    return bad


def _strip_markers(text: str) -> str:
    """去掉渲染侧自动生成的项目符号/前导标记，用于内容匹配。"""
    return text.lstrip("⚫•·▪◦‣⁃●○■□◆◇→").strip()


def _paragraph_block_bottom(words: list, top: float) -> float | None:
    """从某段首行顶向下扩展**同一段落**的换行行，返回该段文字底。

    段内行距实测 18.0–18.05pt；相邻条目最小间距为「条目间距 + ink 高」
    = 5.2 + 13.84 = 19.04pt（原件档案值）或 8.45 + 13.84 = 22.29pt（默认）。
    因此用 18.6pt 作为段内连续行的判定上限：既包含本段换行行，又不会把
    下一条目的首行并进来。缺此扩展时，末段换行 2 行的条目会被截成 1 行。
    """
    tops = sorted({round(_top_of(w), 1) for w in words if _top_of(w) >= top - 0.5})
    if not tops:
        return None
    block = [tops[0]]
    current = tops[0]
    for t in tops[1:]:
        if t - current <= LINE_PITCH_CONTINUATION_PT:
            block.append(t)
            current = t
        else:
            break
    rows = [w for w in words if any(abs(_top_of(w) - t) < 2.0 for t in block)]
    return max(_bottom_of(w) for w in rows) if rows else None


def locate_entry_band(
    words: list, head: str, tail: str, next_head: str | None = None
) -> dict:
    """定位一个条目在渲染页内的 y 区间：{y0, y_end, boundary} 或 {error}。

    边界优先级（P2-R1）：
    ① `next_entry`：同栏目下一条目首行顶（渲染侧独立定位，最可靠）
    ② `own_last_line`：本条**末行**文字底——下一条目首行因 PDF 分词/改写
       无法定位时的兜底；**不能**落回页内 band，否则会把下一条目的文字算进
       本条（行数虚增，跨页/多条目场景会误报）
    ③ `page_band`：页内 band（仅用于栏目内最后一条且末行也无法定位的极端情况）
    """
    y0 = locate_line_top(words, head)
    if y0 is None:
        return {"error": "NOT FOUND"}
    if next_head:
        nxt = locate_line_top(words, next_head)
        if nxt is not None and nxt > y0:
            return {"y0": y0, "y_end": nxt - 0.05, "boundary": "next_entry"}
    tail_top = locate_line_top(words, tail)
    if tail_top is not None and tail_top >= y0 - 0.5:
        block_bottom = _paragraph_block_bottom(words, tail_top)
        if block_bottom is not None:
            return {
                "y0": y0,
                "y_end": block_bottom + 0.05,
                "boundary": "own_last_line",
            }
    band = [w for w in words if _top_of(w) >= y0 - 0.5]
    titles = [
        w for w in band
        if any(_text_of(w).startswith(t) for t in TITLES) and _top_of(w) > y0 + 5
    ]
    if titles:
        limit = min(_top_of(w) for w in titles) - 0.05
        band = [w for w in band if _top_of(w) < limit]
    if not band:
        return {"error": "NOT FOUND"}
    return {
        "y0": y0,
        "y_end": max(_bottom_of(w) for w in band) + 0.05,
        "boundary": "page_band",
    }


def _text_of(w) -> str:
    return w["text"] if isinstance(w, dict) else w[4]


def _top_of(w) -> float:
    return float(w["y0"] if isinstance(w, dict) else w[1])


def _bottom_of(w) -> float:
    return float(w["y1"] if isinstance(w, dict) else w[3])


def locate_line_top(words: list, line: str) -> float | None:
    """在渲染词序列中定位某一行文字的首行顶 y；找不到返回 None。

    同时兼容 fitz 的 tuple（`get_text("words")`：index 4 = 文本）与 dict
    （`rendered_words()`：`text` / `y0`）。PDF 会按字体内部分词（如
    「熟练使用 Python/SQL」被切成两段），单一 10 字前缀匹配会漏——这里逐级
    缩短前缀（10→2 字），任一匹配即命中；仍无命中返回 None。
    """
    needle = _strip_markers(str(line or ""))
    if not needle or not words:
        return None

    for n in (10, 8, 6, 4, 2):
        prefix = needle[:n]
        hits = [
            w for w in words
            if _text_of(w).startswith(prefix)
            or _strip_markers(_text_of(w)).startswith(prefix)
        ]
        if hits:
            return min(_top_of(w) for w in hits)
    return None


def extract_rendered_entries(
    pdf_path: Path, scenario: dict, plan: dict
) -> list[dict]:
    """渲染侧独立提取每条目的首行顶、wrap 行数、**真实占用高度**（R2 Fix2）。

    行数：条目区间内按 18pt 行距带计数（行带聚类，与 COM 测量同语义）。
    占用高度：**末行文字底 − 首行文字顶 + 行内基线偏移 1.85pt**（文字底到
    行框底的差），不做 round(span/18)×18 的取整——行距异常（21pt）会以
    真实差值进入失败门。
    """
    doc = fitz.open(str(pdf_path))
    flat_entries = []  # (page, sec_id, entry_id, head, tail)
    for s in plan["sections"]:
        scen_sec = next(x for x in scenario["sections"] if x["id"] == s["section_id"])
        for e in s["entries"]:
            text_lines = next(
                x["text"].split("\n") for x in scen_sec["entries"]
                if x["id"] == e["instance_id"]
            )
            flat_entries.append((
                e["page_index"], s["section_id"], e["instance_id"],
                text_lines[0], text_lines[-1],
            ))
    out = []
    for idx, (page, sec_id, eid, head, tail) in enumerate(flat_entries):
        pw = doc[page].get_text("words")
        next_head = None
        for p2, s2, e2, h2, _t2 in flat_entries[idx + 1:]:
            if s2 == sec_id and p2 == page:
                next_head = h2
                break
        band = locate_entry_band(pw, head, tail, next_head)
        if band.get("error"):
            out.append({"entry_id": eid, "error": band["error"]})
            continue
        y0 = band["y0"]
        y_end = band["y_end"]
        boundary = band["boundary"]
        # 本条目自己的文字（首行顶到区间终点）——排除区间外文字
        own = [w for w in pw if y0 - 0.5 <= w[1] < y_end]
        if not own:
            out.append({"entry_id": eid, "error": "NOT FOUND"})
            continue
        last_bottom = max(w[3] for w in own)
        span = y_end - y0
        # 行数：按渲染行顶 y 聚类（容差 4pt）——行距不均匀（如 numPr 段
        # 与普通段混排）时除法失准；聚类直接数真实行数
        tops = sorted({round(w[1], 1) for w in own})
        lines = 1
        last_t = tops[0] if tops else y0
        for t in tops[1:]:
            if t - last_t > 4.0:
                lines += 1
                last_t = t
        # 真实占用高度：末行行底 − 首行行顶，行底用「该行文字底 + 行内基线差」。
        # 行内基线差 = 同行文字底最大值与行框底的距离，按 18pt 行距的典型
        # ink 比例（13.8/18）取 4.2pt？——不猜：直接用「首行行顶 → 末行行顶 +
        # 行距」口径会被行距不均影响；统一可比口径 = 文字 ink 跨度 + 固定
        # 行框余量（18−13.8=4.2pt 单行、行间 ink 顶差 4.2*行数）。
        # COM text_height = lines × 18.0（行框口径）；渲染侧行框口径换算：
        # ink 跨度 + (行框余量) —— 单行行框高实测 18.0，ink 高 13.8，
        # 余量 4.2；行框跨度 = ink跨度 + 4.2（首行顶到末行底的单侧余量）
        INK_TO_LINEBOX_PAD = 4.2
        occupied = (last_bottom - y0) + INK_TO_LINEBOX_PAD
        # 实测渲染行距（lines>1 时）：末行顶-首行顶 / (lines-1)。
        # 不假设 18pt——行距异常（如 21pt）通过 pitch 进入失败门。
        line_tops = sorted({t for t in tops})
        pitch = (
            (line_tops[-1] - line_tops[0]) / (lines - 1)
            if lines > 1 and line_tops else None
        )
        out.append({
            "entry_id": eid,
            "page": page + 1,
            "first_line_top_pt": round(y0, 2),
            "wrapped_lines": lines,
            "occupied_height_pt": round(occupied, 2),
            "render_pitch_pt": round(pitch, 2) if pitch is not None else None,
            "render_span_pt": round(span, 2),
            "text_bottom_pt": round(last_bottom, 2),
            "boundary_source": boundary,
        })
    return out


def qa_scenario(
    scen_dir: Path, scenario: dict, plan: dict, measured: dict, *, pdf_name: str,
    header_values: list[str] | None = None,
) -> dict:
    """header_values：本版实际写入个人信息区的值（v2 header.fields）。

    它们既是**预期内容**（必须渲染出来）也是白名单（不属于「多余正文」）；
    否则替换后的姓名/电话会被 no_extra_rendered_content 误判成模板残留。
    """
    pdf = next(scen_dir.glob(f"render/{pdf_name}.pdf"))
    words = rendered_words(pdf)
    issues: list[qa.QAIssue] = []

    # 1. 内容完整性（完整文本 + 顺序 + 重复，游标式）
    rendered_by_page: dict[int, str] = {}
    for r in words:
        rendered_by_page[r["page"]] = rendered_by_page.get(r["page"], "") + r["text"]
    entries_in_order = [
        {"entry_id": e["id"], "text": e["text"]}
        for s in scenario["sections"] for e in s["entries"]
    ]
    issues += qa.check_content_completeness_ordered(
        {p + 1: t for p, t in rendered_by_page.items()}, entries_in_order
    )

    # 2. 页数
    rendered_pages = len({r["page"] for r in words})
    issues += qa.check_page_count(rendered_pages, plan["pages"])

    # 3. 底部越界
    for page in {r["page"] for r in words}:
        for r in words:
            if r["page"] == page and r["y1"] > FOOTER_TOP + 0.5:
                issues.append(qa.QAIssue(
                    "within_page", "error",
                    f"第 {page+1} 页文字底 {r['y1']:.1f}pt 侵入底部装饰条",
                ))
                break

    # 4. 重叠
    for pair in overlap_pairs(words):
        issues.append(qa.QAIssue(
            "no_overlap", "error",
            f"第 {pair['page']} 页文字重叠: {pair['a']} × {pair['b']}",
        ))

    # 5. 标题唯一性
    title_texts = [r["text"] for r in words if any(t in r["text"] for t in TITLES)]
    for t in [s["title"] for s in scenario["sections"]]:
        cnt = sum(1 for x in title_texts if x.startswith(t[:4]))
        if cnt != 1:
            issues.append(qa.QAIssue(
                "title_uniqueness", "error",
                f"栏目标题 {t[:12]}… 出现 {cnt} 次（应为 1）",
            ))

    # 6. 测量 vs 渲染独立对照（失败门；三方 ID 集合校验 + 真实高度对照）
    rendered_entries = extract_rendered_entries(pdf, scenario, plan)
    measured_list = [
        {
            "entry_id": eid,
            "wrapped_lines": m.get("wrapped_lines"),
            "text_height_pt": m.get("text_height_pt"),
            "first_line_top_pt": m.get("first_line_top_pt"),
        }
        for eid, m in measured.items()
    ]
    expected_ids = [e["id"] for s in scenario["sections"] for e in s["entries"]]
    issues += qa.check_measurement_vs_render(
        measured_list, rendered_entries, expected_entry_ids=expected_ids
    )
    err_rows = qa.measurement_error_rows(measured_list, rendered_entries)

    # 7. 额外重复内容（R2 Fix4）：消费完全部预期后残留正文必须为空。
    # 流的取值范围 = 第一栏标题以下的正文区——母版个人信息区（姓名/电话等
    # 模板固定文字）与栏目标题在白名单，不参与残留判定。
    content_stream: dict[int, str] = {}
    for p, t in rendered_by_page.items():
        content_stream[p + 1] = t
    issues += qa.check_no_extra_rendered_content(
        content_stream,
        entries_in_order,
        allowed_extra_needles=[s["title"] for s in scenario["sections"]]
        + _HEADER_WHITELIST
        + [v for v in (header_values or []) if str(v).strip()],
    )

    # 8. 个人信息值必须真的渲染出来（v2 header.fields 的正面校验）
    if header_values:
        stream_norm = qa._norm("".join(content_stream.values()))
        for value in header_values:
            needle = qa._norm(str(value))
            if needle and needle not in stream_norm:
                issues.append(qa.QAIssue(
                    "header_content", "error",
                    f"个人信息值未渲染: {value!r}（header.fields 未被应用）",
                ))

    report = qa.QAReport(
        checks_run=[
            "content_completeness(ordered,full-text)", "page_count", "within_page",
            "no_overlap", "title_uniqueness",
            "measurement_vs_render(fail-gate,id-set+real-height)",
            "no_extra_rendered_content",
        ],
        issues=issues,
        measurement_error_table=err_rows,
    )
    return report.to_dict()


def compare_archive_to_render(pdf_path: Path, archive) -> dict:
    """间距档案（COM 逐段口径） vs 原件实际渲染（PDF ink）对照表（P2-1）。

    COM 口径是**行框**（段落首行顶 / 末段行顶 + 行数×行距）；PDF 口径是
    **字形 ink**（bbox 顶/底）。两者应满足固定符号模式：
    - 首行：ink 顶 ≥ 行框顶（字形顶低于行框顶），差不超 TOL；
    - 末行：ink 底 ≤ 行框底（字形底高于行框底），差不超 TOL。
    分栏边界用渲染侧标题文字定位（不复用 COM 值），保证对照独立。
    """
    tol = 6.0
    doc = fitz.open(str(pdf_path))
    page0 = doc[0].get_text("words")
    title_rows: dict[str, float] = {}
    for w in page0:
        for sid, pref in SECTION_TITLE_PREFIX.items():
            if w[4].startswith(pref):
                title_rows[sid] = min(title_rows.get(sid, 1e9), w[1])
    order = [sid for sid in archive.sections if sid in title_rows]
    rows = []
    for i, sid in enumerate(order):
        band_top = title_rows[sid]
        band_bottom = (
            title_rows[order[i + 1]] if i + 1 < len(order) else FOOTER_TOP
        )
        body = [w for w in page0 if band_top + 5.0 < w[1] < band_bottom]
        if not body:
            rows.append({"section_id": sid, "error": "渲染区间内无正文文字"})
            continue
        ink_top = min(w[1] for w in body)
        ink_bottom = max(w[3] for w in body)
        a = archive.sections[sid]
        top_delta = ink_top - a.text_ink_top_pt
        bottom_delta = ink_bottom - a.text_ink_bottom_pt
        title_delta = title_rows[sid] - a.frame_top_pt - archive.title_text_top_off_pt
        rows.append({
            "section_id": sid,
            "archive_frame_top_pt": round(a.frame_top_pt, 2),
            "render_title_top_pt": round(title_rows[sid], 2),
            "title_top_delta_pt": round(title_delta, 2),
            "archive_ink_top_pt": round(a.text_ink_top_pt, 2),
            "render_ink_top_pt": round(ink_top, 2),
            "ink_top_delta_pt": round(top_delta, 2),
            "archive_ink_bottom_pt": round(a.text_ink_bottom_pt, 2),
            "render_ink_bottom_pt": round(ink_bottom, 2),
            "ink_bottom_delta_pt": round(bottom_delta, 2),
            "within_tol": (
                abs(top_delta) <= tol
                and abs(bottom_delta) <= tol
                and top_delta >= -1.0
                and bottom_delta <= 1.0
                and abs(title_delta) <= tol
            ),
        })
    failed = [r["section_id"] for r in rows if not r.get("within_tol")]
    return {
        "baseline_pdf": str(pdf_path),
        "tolerance_pt": tol,
        "rows": rows,
        "passed": not failed and bool(rows),
        "failed_sections": failed,
    }


def build_preview(scen_dir: Path, baseline_png: Path, pages: list[Path]) -> Path | None:
    """基线第 1 页 + 场景各页并排预览（便于人工/视觉复核同一张图）。"""
    from PIL import Image

    if not baseline_png.is_file():
        return None
    ims = [Image.open(baseline_png).convert("RGB")]
    ims += [Image.open(p).convert("RGB") for p in pages if p.is_file()]
    if len(ims) < 2:
        return None
    gap = 20
    w = sum(i.width for i in ims) + gap * (len(ims) - 1)
    h = max(i.height for i in ims)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    x = 0
    for im in ims:
        canvas.paste(im, (x, 0))
        x += im.width + gap
    out = scen_dir / "preview_with_baseline.png"
    canvas.save(out)
    return out


def main() -> None:
    ev_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from skill_toolbox.resume_layout import generate as G
    from skill_toolbox.resume_layout import spacing as S

    summary = {}
    err_table: list[dict] = []
    artifacts: list[str] = []
    for name in ("replay", "clone", "grow", "twopages"):
        scen_dir = ev_root / name
        if not (scen_dir / "layout_plan.json").is_file():
            continue  # 只汇总已生成的场景（e2e 单场景运行时其余目录不存在）
        scenario = G.SCENARIOS[name]()
        plan = json.loads((scen_dir / "layout_plan.json").read_text(encoding="utf-8"))
        measured_raw = json.loads(
            (scen_dir / "measure_result.json").read_text(encoding="utf-8")
        )
        measured = {r["entry_id"]: r for r in measured_raw["results"]}
        report = qa_scenario(
            scen_dir, scenario, plan, measured, pdf_name=f"resume_{name}"
        )
        (scen_dir / "qa_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for row in report.get("measurement_error_table", []):
            err_table.append({**row, "scenario": name})
        # 基线并排预览（基线第 1 页 + 本场景各页）
        pages = sorted((scen_dir / "render").glob("page-*.png"))
        build_preview(scen_dir, ev_root / "baseline" / "page-1.png", pages)
        summary[name] = {
            "passed": report["passed"],
            "errors": [i["detail"] for i in report["issues"] if i["severity"] == "error"],
            "page_count": plan["pages"],
            "gap_sources": {
                s["section_id"]: s.get("gap_source") for s in plan["sections"]
            },
        }

    if err_table:
        (ev_root / "measurement_error_table.json").write_text(
            json.dumps(err_table, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 间距档案 vs 原件实际渲染（渲染侧独立提取，不复用 COM 值）
    archive_view = None
    archive_path = ev_root / "spacing_archive.json"
    baseline_pdf = ev_root / "baseline" / "template.pdf"
    if archive_path.is_file() and baseline_pdf.is_file():
        archive_view = compare_archive_to_render(
            baseline_pdf, S.load_archive(archive_path)
        )
        (ev_root / "spacing_archive_vs_render.json").write_text(
            json.dumps(archive_view, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    artifacts = [str(p) for p in sorted(ev_root.rglob("*")) if p.is_file()]
    if artifacts:
        (ev_root / "artifacts_index.json").write_text(
            json.dumps(artifacts, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(json.dumps(
        {
            # 兼容旧调用方：场景名在顶层直接可读（tests/test_p1_* 的 e2e 断言）
            **summary,
            "scenarios": summary,
            "archive_vs_render": (
                None if archive_view is None
                else {
                    "passed": archive_view["passed"],
                    "failed_sections": archive_view["failed_sections"],
                    "rows": archive_view["rows"],
                }
            ),
        },
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
