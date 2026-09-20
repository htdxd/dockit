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
        if field.get("link"):
            hyperlink = etree.Element(W + "fldSimple")
            hyperlink.set(W + "instr", f' HYPERLINK "{field["link"]}" ')
            run.addprevious(hyperlink)
            hyperlink.append(run)


def apply_inline_styles(paragraph, fields):
    """拆分标题 run 保留 tab 与基础字号，只对指标文本应用强调。"""
    for field in fields or []:
        text = field["text"]
        for node in list(paragraph.iter(W + "t")):
            if not text or text not in (node.text or ""):
                continue
            run = node.getparent()
            if run.tag != W + "r":
                continue
            # 标题的 w:r 中可能含多个文本与 tab，逐个复制以保持原顺序。
            props = run.find(W + "rPr")
            replacements = []
            for child in run:
                if child.tag == W + "rPr":
                    continue
                parts = (child.text or "").split(text) if child is node else None
                chunks = []
                if parts is None:
                    chunks = [(copy.deepcopy(child), False)]
                else:
                    for i, part in enumerate(parts):
                        if i:
                            match = etree.Element(W + "t")
                            match.text = text
                            chunks.append((match, True))
                        if part:
                            chunk = etree.Element(W + "t")
                            chunk.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                            chunk.text = part
                            chunks.append((chunk, False))
                for chunk, styled in chunks:
                    wrapper = etree.Element(W + "p")
                    replacement = etree.SubElement(wrapper, W + "r")
                    if props is not None:
                        replacement.append(copy.deepcopy(props))
                    replacement.append(chunk)
                    if styled:
                        apply_field_style(wrapper, field, accent=accent_color(paragraph))
                    replacements.extend(list(wrapper))
            for replacement in replacements:
                run.addprevious(replacement)
            run.getparent().remove(run)
            break
