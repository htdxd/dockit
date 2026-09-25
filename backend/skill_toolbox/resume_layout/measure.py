"""在隔离进程中测量已经写入完整样式的正文组件。"""
from skill_toolbox.word_com import (
    close_document,
    close_word,
    start_word,
    text_overflows,
)


def measure_documents(hosts: list[dict]) -> list[dict]:
    rows = []
    word = start_word()
    try:
        for host in hosts:
            doc = word.Documents.Open(host["host"], False, True)
            shape = paras = rng = None
            try:
                doc.Repaginate()
                shape = doc.Shapes("ResumeMeasureBody")
                if text_overflows(shape):
                    raise ValueError(f"LAYOUT_OVERFLOW: 条目 {host['entry_id']} 的 {host['part']} 单框超过一页")
                paras = shape.TextFrame.TextRange.Paragraphs
                tops, counts = [], []
                for i in range(1, paras.Count + 1):
                    rng = paras(i).Range
                    tops.append(float(rng.Characters(1).Information(6)))
                    counts.append(max(1, int(rng.ComputeStatistics(1))))
                if min(tops, default=-1) < 0:
                    raise ValueError(f"MEASURE_INVALID: 条目 {host['entry_id']} 无法测得真实行位")
                pitch = host["line_pitch_pt"]
                bottom = max(top + count * pitch for top, count in zip(tops, counts))
                rows.append({**host, "wrapped_lines": sum(counts), "paragraphs": len(counts),
                             "first_line_top_pt": tops[0], "last_line_top_pt": tops[-1],
                             "text_top_offset_pt": round(tops[0] - host["body_top_pt"], 3),
                             "text_height_pt": round(bottom - tops[0], 3),
                             "text_bottom_offset_pt": round(bottom - host["body_top_pt"], 3)})
            finally:
                rng = paras = shape = None
                close_document(doc)
                doc = None
    finally:
        close_word(word)
    return rows
