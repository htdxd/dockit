"""多区域组件的 PDF 独立验收：逐组件文字、基线、内容守恒与页级碰撞。"""
from collections import Counter

import pymupdf as fitz

from skill_toolbox.resume_layout import qa
from skill_toolbox.resume_layout.header import check_rendered_alignment
from skill_toolbox.resume_layout.report import rendered_words, overlap_pairs


def inspect_component(page, component, measured):
    x, y = component['x_pt'], component['y_pt']
    bounds = fitz.Rect(x, y, x + component['width_pt'], y + component['height_pt'])
    chars = [c for block in page.get_text('rawdict')['blocks']
             for line in block.get('lines', []) for span in line['spans']
             for c in span['chars']
             if bounds.contains(fitz.Point((c['bbox'][0] + c['bbox'][2]) / 2,
                                           (c['bbox'][1] + c['bbox'][3]) / 2))]
    rows = []
    for char in sorted(chars, key=lambda c: (round(c['origin'][1], 1), c['bbox'][0])):
        if not qa._norm(char['c']):
            continue
        baseline = char['origin'][1]
        row = next((r for r in rows if abs(r[0] - baseline) < 1), None)
        if row is None:
            row = [baseline, []]
            rows.append(row)
        row[1].append(char)
    rows.sort(key=lambda r: r[0])
    text = ''.join(c['c'] for _, row in rows for c in sorted(row, key=lambda c: c['bbox'][0]))
    result = {'entry_id': component['measurement_id'], 'wrapped_lines': len(rows)}
    if not rows:
        return text, {**result, 'error': '组件内未提取到正文文字'}
    span = rows[-1][0] - rows[0][0]
    # COM 行框包含末行的完整行距；PDF 基线跨度独立提供此前所有行的占用。
    result.update(occupied_height_pt=span + measured['line_pitch_pt'],
                  render_pitch_pt=span / (len(rows)-1) if len(rows) > 1 else None)
    return text, result


def check_components(pdf_path, scenario, plan, emit_info, measurements, header_components):
    issues, rendered = [], []
    components = emit_info['components']
    measured_by_id = {m['entry_id']: m for m in measurements}
    expected_entries = {e['id']: e['text'] for s in scenario['sections'] for e in s['entries']}
    actual_ids = {c['entry_id'] for c in components}
    if actual_ids != set(expected_entries):
        issues.append(qa.QAIssue('component_contract', 'error', '组件条目集合与输入不一致'))
    for eid, text in expected_entries.items():
        parts = ''.join(c['text'] for c in components if c['entry_id'] == eid)
        if Counter(qa._norm(parts)) != Counter(qa._norm(text)):
            issues.append(qa.QAIssue('component_content', 'error', f'{eid} 拆分组件丢失或重复了输入文字'))
    with fitz.open(pdf_path) as doc:
        issues += qa.check_page_count(len(doc), plan['pages'])
        rendered_lines = Counter(qa._norm(''.join(s['text'] for s in line['spans']))
            for page in doc for block in page.get_text('dict')['blocks']
            for line in block.get('lines', []))
        for title, count in Counter(qa._norm(s['title']) for s in scenario['sections']).items():
            if rendered_lines[title] != count:
                issues.append(qa.QAIssue('title_uniqueness', 'error',
                    f'栏目标题 {title} 渲染 {rendered_lines[title]} 次，预期 {count} 次'))
        for c in components:
            mid = c['measurement_id']
            if mid not in measured_by_id or c['page_index'] >= len(doc):
                issues.append(qa.QAIssue('component_contract', 'error', f'{mid} 缺少测量或页面'))
                continue
            text, row = inspect_component(doc[c['page_index']], c, measured_by_id[mid])
            rendered.append(row)
            if qa._norm(text) != qa._norm(c['text']):
                issues.append(qa.QAIssue('content_completeness', 'error', f"{mid} 渲染文字缺失、乱序或重复：{text[:80]!r}"))
            page = doc[c['page_index']]
            if c['x_pt'] < -0.1 or c['y_pt'] < -0.1 or c['x_pt'] + c['width_pt'] > page.rect.width + 0.1 or c['y_pt'] + c['height_pt'] > plan['page_capacity_bottom_pt'] + 0.1:
                issues.append(qa.QAIssue('within_page', 'error', f'{mid} 组件超出页面可用范围'))
        full_text = ''.join(page.get_text() for page in doc)
        expected_text = ''.join(c['text'] for c in components) + ''.join(emit_info['fixed_texts'])
        actual, expected = Counter(qa._norm(full_text)), Counter(qa._norm(expected_text))
        if actual != expected:
            issues.append(qa.QAIssue('no_extra_rendered_content', 'error',
                f"全文内容不守恒：缺失 {dict(expected-actual)}；多余 {dict(actual-expected)}"))
    issues += check_rendered_alignment(pdf_path, header_components)
    for pair in overlap_pairs(rendered_words(pdf_path)):
        issues.append(qa.QAIssue('no_overlap', 'error', f"第 {pair['page']} 页文字重叠：{pair['a']} × {pair['b']}"))
    issues += qa.check_measurement_vs_render(measurements, rendered,
        expected_entry_ids=[c['measurement_id'] for c in components])
    return qa.QAReport(checks_run=['component_content', 'content_completeness', 'page_count',
        'within_page', 'no_overlap', 'no_extra_rendered_content', 'header_alignment', 'title_uniqueness',
        'measurement_vs_render'], issues=issues,
        measurement_error_table=qa.measurement_error_rows(measurements, rendered)).to_dict()
