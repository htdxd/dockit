"""测量与输出共用的局部强调及超链接，不改变内容或任意定位。"""
from lxml import etree
import copy

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def accent_color(node):
    # 两套已认证模板的主题色均来源于真实标题，未指定时使用稳妥的深蓝。
    colors = [c.get(W + "val", "") for c in node.getroottree().getroot().iter(W + "color")]
    return next((c for c in colors if len(c) == 6 and c.upper() not in {"000000", "FFFFFF", "808080", "595959"}
                 and c[:2].upper() != c[2:4].upper()), "244761")


def apply_field_style(paragraph, field, *, value_only=False, accent=None):
    emphasis = field.get("emphasis", "normal")
    runs = list(paragraph.iter(W + "r"))
    if value_only:
        runs = runs[1:]
    for run in runs:
        props = run.find(W + "rPr")
        if props is None:
            props = etree.Element(W + "rPr")
            run.insert(0, props)
        if emphasis in {"bold", "bold_accent"}:
            for tag in ("b", "bCs"):
                old = props.find(W + tag)
                if old is None:
                    old = etree.SubElement(props, W + tag)
                old.set(W + "val", "1")
        if emphasis in {"accent", "bold_accent"}:
            # Word 优先采用模板的 DrawingML 文字填充；仅改 w:color 不会生效。
            for fill in props.findall("{http://schemas.microsoft.com/office/word/2010/wordml}textFill"):
                props.remove(fill)
            old = props.find(W + "color")
            if old is None:
                old = etree.SubElement(props, W + "color")
            old.attrib.clear()
            old.set(W + "val", accent or accent_color(paragraph))
        if field.get("background"):
            shade = props.find(W + "shd")
            if shade is None:
                shade = etree.SubElement(props, W + "shd")
            shade.attrib.clear()
            color = accent or accent_color(paragraph)
            fill = ''.join(f'{round(int(color[i:i+2], 16) * .14 + 255 * .86):02X}' for i in (0, 2, 4))
            shade.set(W + "val", "clear")
            shade.set(W + "fill", fill)
        if field.get("link"):
            hyperlink = etree.Element(W + "fldSimple")
            hyperlink.set(W + "instr", f' HYPERLINK "{field["link"]}" ')
            run.addprevious(hyperlink)
            hyperlink.append(run)


def apply_inline_styles(paragraph, fields):
    """旧标题指标入口也使用统一的字符范围排版。"""
    text = ''.join((node.text or '') if node.tag == W + 't' else '\t'
                   for run in paragraph.iter(W + 'r') for node in run
                   if node.tag in {W + 't', W + 'tab'})
    spans = []
    for field in fields or []:
        fragment = field['text']
        start = text.find(fragment)
        if fragment and start >= 0:
            spans.append({**field, 'start': start, 'end': start + len(fragment)})
    if spans:
        apply_text_spans(paragraph, spans)


def apply_text_spans(paragraph, spans):
    """按整段字符位置拆分 run，保留 tab、链接和原格式；测量与输出共用。"""
    accent = accent_color(paragraph)
    cursor = 0
    for run in list(paragraph.iter(W + "r")):
        props = run.find(W + "rPr")
        replacements = []
        for child in run:
            if child.tag == W + "rPr":
                continue
            length = len(child.text or "") if child.tag == W + "t" else int(child.tag == W + "tab")
            cuts = {0, length}
            for span in spans:
                cuts.update(max(0, min(length, span[k] - cursor)) for k in ("start", "end"))
            cuts = sorted(cuts)
            chunks = list(zip(cuts, cuts[1:])) if length else [(0, 0)]
            for start, end in chunks:
                wrapper = etree.Element(W + "p")
                new = etree.SubElement(wrapper, W + "r")
                if props is not None:
                    new.append(copy.deepcopy(props))
                node = copy.deepcopy(child)
                if node.tag == W + "t":
                    node.text = (child.text or "")[start:end]
                    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                new.append(node)
                for span in spans:
                    if length and span["start"] <= cursor + start and cursor + end <= span["end"]:
                        apply_field_style(wrapper, span, accent=accent)
                replacements.extend(list(wrapper))
            cursor += length
        for replacement in replacements:
            run.addprevious(replacement)
        run.getparent().remove(run)
