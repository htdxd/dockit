"""条目字号与组件缩放的共享 XML 实现；测量宿主和最终 DOCX 使用同一路径。"""
from __future__ import annotations

import copy
import zipfile
from pathlib import Path

from skill_toolbox.resume_layout import emit
from skill_toolbox.resume_layout.profiles import get_profile


def factors(entry: dict, section: dict, template_id: str) -> tuple[float, float]:
    base = get_profile(template_id).base_font_pt
    geometry = float(entry.get("scale") or 1) * float(section.get("scale") or 1)
    font = float(entry.get("font_size_pt") or base) / base * geometry
    if base * font < 8.0 - 1e-6:
        raise ValueError(f"TYPOGRAPHY_BOUNDS: 缩放后的实际正文字号 {base * font:.2f}pt 小于可读下限 8pt")
    return font, geometry


def line_pitch(entry: dict, section: dict, template_id: str) -> float:
    font, _ = factors(entry, section, template_id)
    profile = get_profile(template_id)
    pitch = float(section.get('line_pitch_pt', profile.line_pitch_pt))
    return round((profile.base_font_pt * 1.5 if section.get("density") == "compact" else pitch) * font * 20) / 20


def apply_density(content: dict, density: str, template_id: str) -> None:
    """紧凑模式展开均匀缩放，保留等效字号和全部内容，恢复模板宽度。"""
    if density == "compact":
        base = get_profile(template_id).base_font_pt
        for section in content["sections"]:
            for entry in section["entries"]:
                font, _ = factors(entry, section, template_id)
                entry["font_size_pt"] = round(base * font * 2) / 2
                entry["scale"] = 1.0
            section["scale"] = 1.0
    content["density"] = density


def _scale_text(root, factor: float, base: float):
    if abs(factor - 1) < 1e-9:
        return
    for props in root.iter(emit.W + "rPr"):
        for key in ("sz", "szCs"):
            size = props.find(emit.W + key)
            if size is None:
                size = emit.etree.SubElement(props, emit.W + key)
                size.set(emit.W + "val", str(round(base * 2)))
            size.set(emit.W + "val", str(round(float(size.get(emit.W + "val")) * factor)))


def _scale_metrics(root, factor: float):
    if abs(factor - 1) < 1e-9:
        return
    for node in root.iter():
        if node.tag == emit.W + "ind":
            for key in ("left", "right", "firstLine", "hanging", "start", "end"):
                name = emit.W + key
                if node.get(name) is not None:
                    node.set(name, str(round(float(node.get(name)) * factor)))
        elif node.tag == emit.W + "tab" and node.get(emit.W + "pos") is not None:
            node.set(emit.W + "pos", str(round(float(node.get(emit.W + "pos")) * factor)))


class Numbering:
    """为有不同缩放的条目复制其编号样式，避免修改其它条目的 bullet。"""
    def __init__(self, template: Path):
        with zipfile.ZipFile(template) as package:
            self.root = (emit.etree.fromstring(package.read("word/numbering.xml"))
                         if "word/numbering.xml" in package.namelist()
                         else emit.etree.Element(emit.W + "numbering"))
        self.cache = {}
        self.changed = False

    def apply(self, body, font_factor: float, geometry_factor: float, base: float):
        if abs(font_factor - 1) < 1e-9 and abs(geometry_factor - 1) < 1e-9:
            return
        for ref in body.findall(".//" + emit.W + "numPr/" + emit.W + "numId"):
            old_id = ref.get(emit.W + "val")
            key = (old_id, round(font_factor, 6), round(geometry_factor, 6))
            if key not in self.cache:
                num = next(n for n in self.root.findall(emit.W + "num")
                           if n.get(emit.W + "numId") == old_id)
                abstract_id = num.find(emit.W + "abstractNumId").get(emit.W + "val")
                abstract = copy.deepcopy(next(a for a in self.root.findall(emit.W + "abstractNum")
                    if a.get(emit.W + "abstractNumId") == abstract_id))
                new_abs = str(1 + max(int(a.get(emit.W + "abstractNumId")) for a in self.root.findall(emit.W + "abstractNum")))
                new_num = str(1 + max(int(n.get(emit.W + "numId")) for n in self.root.findall(emit.W + "num")))
                abstract.set(emit.W + "abstractNumId", new_abs)
                _scale_text(abstract, font_factor, base)
                _scale_metrics(abstract, geometry_factor)
                cloned = copy.deepcopy(num)
                cloned.set(emit.W + "numId", new_num)
                cloned.find(emit.W + "abstractNumId").set(emit.W + "val", new_abs)
                self.root.insert(0, abstract)
                self.root.append(cloned)
                self.cache[key] = new_num
                self.changed = True
            ref.set(emit.W + "val", self.cache[key])

    def parts(self) -> dict[str, bytes]:
        return {"word/numbering.xml": emit.etree.tostring(self.root, encoding="utf-8", xml_declaration=True)} if self.changed else {}


def scale_anchor(anchor, factor: float):
    """缩放内部坐标域及外框，保持页面锚点；子组比例不被重复乘入。"""
    if abs(factor - 1) < 1e-9:
        return
    for transform in anchor.iter(emit.A + "xfrm"):
        for child in transform:
            for key in ("x", "y", "cx", "cy"):
                if child.get(key) is not None:
                    child.set(key, str(round(int(child.get(key)) * factor)))
    extent = anchor.find(emit.WP + "extent")
    for key in ("cx", "cy"):
        extent.set(key, str(round(int(extent.get(key)) * factor)))
    for line in anchor.iter(emit.A + "ln"):
        if line.get("w") is not None:
            line.set("w", str(round(int(line.get("w")) * factor)))


def scale_title(anchor, factor: float, *, body=None, template_id: str):
    """栏目标题、图标、横线一起缩放；正文文字另按条目设置。"""
    position = anchor.find(emit.WP + "positionH")
    left = int(position.find(emit.WP + "posOffset").text) / emit.EMU
    if position.get("relativeFrom") == "column":
        left += 90.0 if template_id == "t001" else 42.0
    width = int(anchor.find(emit.WP + "extent").get("cx")) / emit.EMU * factor
    if left + width > 595.3 + 0.1:
        raise ValueError("TYPOGRAPHY_BOUNDS: 栏目标题/图标缩放后越过页面右边界")
    scale_anchor(anchor, factor)
    if abs(factor - 1) < 1e-9:
        return
    for box in anchor.iter(emit.WPS + "wsp"):
        if box is body:
            continue
        _scale_text(box, factor, 12.0 if template_id == "t001" else 13.0)
        _scale_metrics(box, factor)
        for spacing in box.iter(emit.W + "spacing"):
            for key in ("before", "after", "line"):
                if key == "line" and spacing.get(emit.W + "lineRule") not in ("exact", "atLeast"):
                    continue
                if spacing.get(emit.W + key) is not None:
                    spacing.set(emit.W + key, str(round(float(spacing.get(emit.W + key)) * factor)))
        props = box.find(emit.WPS + "bodyPr")
        if props is not None:
            for key, default in (("lIns", 91440), ("rIns", 91440), ("tIns", 45720), ("bIns", 45720)):
                props.set(key, str(round(int(props.get(key, default)) * factor)))


def configure_heading_tabs(body, entry: dict, *, template_id: str = "t109"):
    if not entry.get("has_heading") or "\t" not in entry["text"].split("\n", 1)[0]:
        return
    p = body.find(".//" + emit.W + "txbxContent/" + emit.W + "p")
    ppr = p.find(emit.W + "pPr")
    if ppr is None:
        ppr = emit.etree.Element(emit.W + "pPr")
        p.insert(0, ppr)
    old = ppr.find(emit.W + "tabs")
    if old is not None:
        ppr.remove(old)
    tabs = emit.etree.Element(emit.W + "tabs")
    ppr.insert(1 if ppr.find(emit.W + "pStyle") is not None else 0, tabs)
    extent = body.find(emit.WPS + "spPr/" + emit.A + "xfrm/" + emit.A + "ext")
    props = body.find(emit.WPS + "bodyPr")
    width = (int(extent.get("cx")) - int(props.get("lIns", 91440))
             - int(props.get("rIns", 91440))) / emit.EMU
    stops = ([("center", width / 2), ("right", width)]
             if entry["text"].split("\n", 1)[0].count("\t") > 1
             else [("right", width)] if template_id == "t001" else [("left", width * 0.6)])
    for align, position in stops:
        tab = emit.etree.SubElement(tabs, emit.W + "tab")
        tab.set(emit.W + "val", align)
        tab.set(emit.W + "pos", str(round(position * 20)))


def apply_body(body, entry: dict, section: dict, template_id: str, numbering: Numbering) -> dict:
    """调用前栏目 anchor 已按 section.scale 缩放，body 只再缩放 entry.scale。"""
    profile = get_profile(template_id)
    font, geometry = factors(entry, section, template_id)
    base = profile.base_font_pt
    extent = body.find(emit.WPS + "spPr/" + emit.A + "xfrm/" + emit.A + "ext")
    width = float(entry.get("width_pt") or (int(extent.get("cx")) / emit.EMU * float(entry.get("scale") or 1)))
    original_width = int(extent.get("cx")) / emit.EMU / float(section.get("scale") or 1)
    maximum_width = max(profile.body_width_pt, original_width)
    if width > maximum_width + 0.01:
        raise ValueError(f"TYPOGRAPHY_BOUNDS: 缩放后正文宽度 {width:.2f}pt 超过可用 {maximum_width:.2f}pt")
    emit.set_child_width(body, width)
    extent.set("cy", str(round(int(extent.get("cy")) * float(entry.get("scale") or 1))))
    props = body.find(emit.WPS + "bodyPr")
    if abs(geometry - 1) > 1e-9:
        for key, default in (("lIns", 91440), ("rIns", 91440), ("tIns", 45720), ("bIns", 45720)):
            props.set(key, str(round(int(props.get(key, default)) * geometry)))
    _scale_text(body, font, base)
    _scale_metrics(body, geometry)
    compact = section.get("density") == "compact"
    pitch = line_pitch(entry, section, template_id)
    if compact:
        props.set("tIns", str(round(1.8 * emit.EMU)))
        props.set("bIns", str(round(1.8 * emit.EMU)))
    if abs(font - 1) > 1e-9 or compact:
        for p in body.findall(".//" + emit.W + "txbxContent/" + emit.W + "p"):
            ppr = p.find(emit.W + "pPr")
            if ppr is None:
                ppr = emit.etree.Element(emit.W + "pPr")
                p.insert(0, ppr)
            spacing = ppr.find(emit.W + "spacing")
            if spacing is None:
                spacing = emit.etree.SubElement(ppr, emit.W + "spacing")
            for key in ("before", "after"):
                if spacing.get(emit.W + key) is not None:
                    spacing.set(emit.W + key, "0" if compact else str(round(float(spacing.get(emit.W + key)) * font)))
            spacing.set(emit.W + "line", str(round(pitch * 20)))
            spacing.set(emit.W + "lineRule", "exact")
    numbering.apply(body, font, geometry, base)
    configure_heading_tabs(body, entry, template_id=template_id)
    return {"body_width_pt": width, "line_pitch_pt": pitch,
            "font_factor": font, "body_pad_pt": 1.8 if compact else profile.geometry.body_pad_pt * geometry,
            "body_offset_pt": float(section.get("body_offset_pt", profile.geometry.body_offset_pt)) * float(section.get("scale") or 1)}
