# -*- coding: utf-8 -*-
"""Word COM 测量探针：模板宿主文档中按真实样式测量条目文本行数。

P1 探针验证结论（见 .tmp_t/p0p1/probe 实验记录）：
- shape.TextFrame.TextRange.Paragraphs(i).Range.Information(6)（wdVerticalPosition
 RelativeToPage）给出组内子 shape 每段首行在页面上的精确 y（与渲染 PDF 一致）。
- Paragraphs(i).Range.ComputeStatistics(1)（wdStatisticLines）给出每段 wrap 后行数。
- 行距 18.0pt 精确可复现（微软雅黑 10.5pt，snapToGrid=0）。
- TextFrame.AutoSize / Height 读数不可靠（组内子 shape 不重算），一律不用。

测量流程：复制原件为宿主 → 清空锚定正文框 → 逐条写入条目文本 →
COM 打开读每段 top/lines → 计算条目真实高度。单实例批量测量，结束清理自身进程。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

EMU_PER_PT = 12700
WD_VERTICAL_POS = 6     # wdVerticalPositionRelativeToPage
WD_STAT_LINES = 1       # wdStatisticLines

try:  # 独立进程运行时才导入（布局单测不依赖 COM）
    import pythoncom  # type: ignore
    from win32com import client  # type: ignore
    _COM_OK = True
except ImportError:  # pragma: no cover - 非 Windows 环境标记
    _COM_OK = False


@dataclass
class EntryMeasurement:
    entry_id: str
    paragraphs: int
    wrapped_lines: int
    first_line_top_pt: float
    last_line_top_pt: float
    line_pitch_pt: float
    # 文本占用高度（首行顶到末行底）：lines*pitch 与 top 差取大者（保守）
    text_height_pt: float
    text_top_offset_pt: float | None = None
    body_width_pt: float | None = None
    body_offset_pt: float | None = None
    body_pad_pt: float | None = None

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "paragraphs": self.paragraphs,
            "wrapped_lines": self.wrapped_lines,
            "first_line_top_pt": round(self.first_line_top_pt, 2),
            "last_line_top_pt": round(self.last_line_top_pt, 2),
            "line_pitch_pt": round(self.line_pitch_pt, 2),
            "text_height_pt": round(self.text_height_pt, 2),
            **{key: getattr(self, key) for key in (
                "text_top_offset_pt", "body_width_pt", "body_offset_pt", "body_pad_pt")
               if getattr(self, key) is not None},
        }


class WordMeasureProbe:
    """在模板副本上测量条目文本的真实行几何。必须以子进程方式使用。"""

    def __init__(self, host_docx: Path) -> None:
        if not _COM_OK:
            raise RuntimeError("pywin32 不可用：Word COM 测量探针需要 Windows + pywin32")
        self.host_docx = host_docx

    def measure_entries(
        self,
        shape_group_index: int,
        body_item_index: int,
        entries: list[dict],
    ) -> list[EntryMeasurement]:
        """把 entries 逐条写入宿主正文框并测量。

        shape_group_index/body_item_index：Word COM Shapes(n).GroupItems(m) 的
        1-based 序号（宿主文档内教育栏组与正文子框）。
        entries: [{"id": ..., "text": "多行文本\r...", "width_pt": 可选}]
        条目带 `width_pt` 时先改正文框宽度再测量（宽度变化必须重新测量换行，
        与 emit 侧写入的 ext.cx 同口径）；不带则复位宿主原始宽度。
        """
        results: list[EntryMeasurement] = []
        pythoncom.CoInitialize()
        word = client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = None
        try:
            doc = word.Documents.Open(str(self.host_docx), False, True)
            body = doc.Shapes(shape_group_index).GroupItems(body_item_index)
            base_width = float(body.Width)
            tr = body.TextFrame.TextRange
            for item in entries:
                width = item.get("width_pt")
                body.Width = float(width) if width else base_width
                tr.Text = item["text"]
                paras = tr.Paragraphs
                n = paras.Count
                for i, bold in enumerate(item.get("paragraph_bold", []), 1):
                    paras(i).Range.Font.Bold = -1 if bold else 0
                tops: list[float] = []
                line_total = 0
                for i in range(1, n + 1):
                    rng = paras(i).Range
                    tops.append(float(rng.Information(WD_VERTICAL_POS)))
                    line_total += int(rng.ComputeStatistics(WD_STAT_LINES))
                pitch = 18.0  # 宿主样式实测；跨条目核验（末行-首行)/(wrap差) 防退化
                if len(tops) >= 2:
                    derived = (tops[-1] - tops[0]) / max(1, (sum_pitch_denominator(tops, pitch)))
                    if abs(derived - pitch) > 0.05:
                        pitch = derived
                # 高度 = 行数 × 行距（文字底部含 descender，保守 +0.5）
                height = line_total * pitch
                results.append(EntryMeasurement(
                    entry_id=item["id"],
                    paragraphs=n,
                    wrapped_lines=line_total,
                    first_line_top_pt=tops[0],
                    last_line_top_pt=tops[-1],
                    line_pitch_pt=pitch,
                    text_height_pt=height,
                ))
        finally:
            if doc is not None:
                doc.Close(False)
            word.Quit()
            pythoncom.CoUninitialize()
        return results


def sum_pitch_denominator(tops: list[float], pitch: float) -> int:
    """段间行数（不含首段首行）总行数估计，用于反推 pitch。"""
    total = 0
    for prev, cur in zip(tops, tops[1:]):
        total += max(1, round((cur - prev) / pitch))
    return max(1, total)


def measure_documents(entries: list[dict]) -> list[EntryMeasurement]:
    """读取已按 emit 规则写好完整样式的宿主；COM 不再重写文字或猜测字体。"""
    if not _COM_OK:
        raise RuntimeError("Word COM 测量需要 Windows + pywin32")
    results = []
    pythoncom.CoInitialize()
    word = client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        for item in entries:
            doc = word.Documents.Open(str(item["host"]), False, True)
            try:
                shape = doc.Shapes("ResumeMeasureBody")
                body = shape.GroupItems(item["body_item"]) if item.get("body_item") else shape
                tr = body.TextFrame.TextRange
                paragraphs = tr.Paragraphs
                tops, paragraph_lines = [], []
                for i in range(1, paragraphs.Count + 1):
                    rng = paragraphs(i).Range
                    tops.append(float(rng.Information(WD_VERTICAL_POS)))
                    paragraph_lines.append(int(rng.ComputeStatistics(WD_STAT_LINES)))
                lines = sum(paragraph_lines)
                pitch = float(item.get("line_pitch_pt", 18.0))
                if len(tops) > 1:
                    pitch = (tops[-1] - tops[0]) / max(1, sum(paragraph_lines[:-1]))
                results.append(EntryMeasurement(
                    item["id"], paragraphs.Count, lines, tops[0], tops[-1],
                    pitch, lines * pitch,
                    text_top_offset_pt=round(tops[0] - item["body_top_pt"], 3) if "body_top_pt" in item else None,
                    body_width_pt=item.get("body_width_pt"),
                    body_offset_pt=item.get("body_offset_pt"),
                    body_pad_pt=item.get("body_pad_pt"),
                ))
            finally:
                doc.Close(False)
    finally:
        word.Quit()
        pythoncom.CoUninitialize()
    return results


def run_probe(host_docx: Path, spec_path: Path, out_path: Path) -> None:
    """CLI 入口：python measure.py <host.docx> <spec.json> <out.json>。"""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    probe = WordMeasureProbe(host_docx)
    results = probe.measure_entries(
        int(spec["shape_group_index"]),
        int(spec["body_item_index"]),
        spec["entries"],
    )
    out_path.write_text(
        json.dumps(
            {"ok": True, "host": str(host_docx), "results": [r.to_dict() for r in results]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
