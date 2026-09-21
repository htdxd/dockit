"""模板定义、OOXML 组件几何与分区排版入口。"""
from __future__ import annotations

import copy
import importlib
from pathlib import Path

from skill_toolbox.resume_layout import emit

TEMPLATE_IDS = frozenset({'t001', 't002', 't003', 't015', 't024', 't109'})
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'


def get_spec(template_id):
    return importlib.import_module(f'skill_toolbox.resume_layout.{template_id}').SPEC


def source_key(section, spec):
    key = section['id']
    if key in spec['sections']:
        return key
    if section.get('prototype') == 'plain_lines_v1':
        return 'skills'
    return 'work' if 'work' in spec['sections'] else 'internship'


def prepare_sections(sections, template_id):
    spec = get_spec(template_id)
    for section in sections:
        source = spec['sections'][source_key(section, spec)]
        section['region'] = source['region']
        section['body_offset_pt'] = source['body_offset_pt']
        section['line_pitch_pt'] = source.get('line_pitch_pt', spec['line_pitch_pt'])


def plan_scenario(scenario, measurements, *, template_id, geometry, **options):
    """每个排版区域独立推进，共享现有条目分页器与高度约束。"""
    from dataclasses import replace

    from skill_toolbox.resume_layout import layout
    spec = get_spec(template_id)
    fit = (scenario.get('header') or {}).get('fit') or {}
    region_tops = fit.get('body_top_by_region') or {}
    plans = []
    for name, region in spec['regions'].items():
        sections = [s for s in scenario['sections'] if s['region'] == name]
        if not sections:
            continue
        top = region_tops.get(name, fit.get('body_top_pt', region['top_pt'])
                              if name == 'main' else region['top_pt'])
        regional_geometry = replace(geometry, page_top_pt=top,
            continuation_page_top_pt=region['continuation_top_pt'],
            page_bottom_pt=max(region['bottom_pt'], geometry.page_bottom_pt))
        plan = layout.plan_layout(sections, measurements, geometry=regional_geometry, **options)
        for section in plan.sections:
            section.region = name
            for entry in section.entries:
                entry.body_x_pt = region['x_pt']
        plans.append(plan)
    by_id = {s.section_id: s for p in plans for s in p.sections}
    return layout.LayoutPlan(max(p.pages for p in plans),
        [by_id[s['id']] for s in scenario['sections']],
        min(p.page_capacity_top_pt for p in plans), geometry.page_bottom_pt,
        geometry.continuation_page_top_pt)


def ensure_archive(template, path, template_id):
    """未校准栏目使用分页器的显式通用间距，不冒充原件测量档案。"""
    import hashlib

    from skill_toolbox.resume_layout.spacing import (
        SCHEMA_VERSION,
        SpacingArchive,
        load_archive,
        save_archive,
    )
    calibrated = template.with_name('spacing.json')
    if calibrated.is_file():
        archive = load_archive(calibrated)
        if archive.template_sha256 != hashlib.sha256(template.read_bytes()).hexdigest():
            raise ValueError('模板已变化，请重新校准间距档案')
        save_archive(archive, path)
        return archive
    archive = SpacingArchive(template_id, hashlib.sha256(template.read_bytes()).hexdigest(),
        SCHEMA_VERSION, get_spec(template_id)['title_text_top_pt'])
    save_archive(archive, path)
    return archive


def anchor(root, ident):
    return next(a for a in root.iter(emit.WP+'anchor')
                if a.find(emit.WP+'docPr').get('id') == str(ident))


def set_position(node, *, x=None, y=None):
    for axis, value in [('H', x), ('V', y)]:
        if value is None:
            continue
        position = node.find(emit.WP+'position'+axis)
        position.set('relativeFrom', 'page')
        for child in list(position):
            position.remove(child)
        emit.etree.SubElement(position, emit.WP+'posOffset').text = str(emit.pt2emu(value))


def set_extent(node, *, width=None, height=None):
    extent = node.find(emit.WP+'extent')
    shape = next(node.iter(emit.WPS+'wsp'), None)
    transform = shape.find(emit.WPS+'spPr/'+emit.A+'xfrm') if shape is not None else None
    for key, value in [('cx', width), ('cy', height)]:
        if value is None:
            continue
        extent.set(key, str(emit.pt2emu(value)))
        if transform is not None:
            transform.find(emit.A+'ext').set(key, str(emit.pt2emu(value)))


def position(node):
    return tuple(int(node.find(emit.WP+'position'+axis+'/'+emit.WP+'posOffset').text)/emit.EMU
                 for axis in ('H','V'))


def extent(node):
    return tuple(int(node.find(emit.WP+'extent').get(key))/emit.EMU for key in ('cx','cy'))


def append_anchor(root, node, carrier=None):
    if carrier is None:
        carrier = root.find(emit.W+'body/'+emit.W+'p')
    next_id = max(emit._scan_max(root, 'docPr', 'id'), emit._scan_max(root, 'cNvPr', 'id')) + 1
    emit.uniquify_ids(root, node, next_id)
    run = emit.etree.SubElement(carrier, emit.W+'r')
    emit.etree.SubElement(run, emit.W+'drawing').append(node)
    return node


def load_normalized(template: Path, spec):
    root = emit.load_document_xml(template)
    for fallback in list(root.iter(MC+'Fallback')):
        fallback.getparent().remove(fallback)
    anchors = list(root.iter(emit.WP+'anchor'))
    for item in anchors:
        ident = int(item.find(emit.WP+'docPr').get('id'))
        if 'positions' in spec:
            x, y = spec['positions'][ident]
        else:
            x, y = (int(item.find(emit.WP+'position'+axis+'/'+emit.WP+'posOffset').text) / emit.EMU
                    + spec.get('origin', {}).get(item.find(emit.WP+'position'+axis).get('relativeFrom'), 0)
                    for axis in ('H', 'V'))
        set_position(item, x=x, y=y)
        item.getparent().remove(item)
    body = root.find(emit.W+'body')
    for item in list(body):
        if item.tag != emit.W+'sectPr':
            body.remove(item)
    carrier = emit.etree.Element(emit.W+'p')
    body.insert(0, carrier)
    props = emit.etree.SubElement(carrier, emit.W+'pPr')
    emit.etree.SubElement(props, emit.W+'spacing', {emit.W+'before':'0', emit.W+'after':'0',
                                                   emit.W+'line':'20', emit.W+'lineRule':'exact'})
    for item in anchors:
        run = emit.etree.SubElement(carrier, emit.W+'r')
        emit.etree.SubElement(run, emit.W+'drawing').append(item)
    return root


def _shape_bounds(container):
    """累积嵌套组的坐标系；不能把子 shape 的 off/ext 直接当页面 pt。"""
    x, y = position(container)
    width, height = extent(container)
    data = container.find(emit.A+'graphic/'+emit.A+'graphicData')
    first = data[0]
    if first.tag == emit.WPS+'wsp':
        return [(first, (x, y, width, height))]
    result = []

    def pair(node, name, keys, default):
        item = node.find(emit.A+name)
        return tuple(float(item.get(k)) for k in keys) if item is not None else default

    def walk(group, ox, oy, sx, sy, outer=False):
        transform = group.find(emit.WPG+'grpSpPr/'+emit.A+'xfrm')
        off = pair(transform, 'off', ('x','y'), (0,0))
        size = pair(transform, 'ext', ('cx','cy'), (1,1))
        child_off = pair(transform, 'chOff', ('x','y'), (0,0))
        child_size = pair(transform, 'chExt', ('cx','cy'), size)
        if outer:
            sx, sy = width/child_size[0], height/child_size[1]
            ox, oy = x-child_off[0]*sx, y-child_off[1]*sy
        else:
            nx, ny = sx*size[0]/child_size[0], sy*size[1]/child_size[1]
            ox, oy = ox+off[0]*sx-child_off[0]*nx, oy+off[1]*sy-child_off[1]*ny
            sx, sy = nx, ny
        for child in group:
            if child.tag in {emit.WPG+'wgp', emit.WPG+'grpSp'}:
                walk(child, ox, oy, sx, sy)
            elif child.tag == emit.WPS+'wsp':
                transform = child.find(emit.WPS+'spPr/'+emit.A+'xfrm')
                off = pair(transform, 'off', ('x','y'), (0,0))
                size = pair(transform, 'ext', ('cx','cy'), (1,1))
                result.append((child, (ox+off[0]*sx, oy+off[1]*sy, size[0]*sx, size[1]*sy)))
    walk(first, 0, 0, 1, 1, True)
    return result


def box_anchor(root, ref):
    original = anchor(root, ref['anchor'])
    shapes = _shape_bounds(original)
    shape, (x, y, width, height) = shapes[ref.get('shape') or 0]
    node = copy.deepcopy(original)
    graphic = node.find(emit.A+'graphic/'+emit.A+'graphicData')
    for child in list(graphic):
        graphic.remove(child)
    graphic.set('uri', emit.WPS[1:-1])
    shape = copy.deepcopy(shape)
    graphic.append(shape)
    transform = shape.find(emit.WPS+'spPr/'+emit.A+'xfrm')
    transform.find(emit.A+'off').set('x', '0')
    transform.find(emit.A+'off').set('y', '0')
    set_position(node, x=x, y=y)
    set_extent(node, width=width, height=height)
    return node


def strip_boxes(root, refs):
    selected = []
    for ref in refs:
        original = anchor(root, ref['anchor'])
        selected.append(original if ref.get('shape') is None else list(original.iter(emit.WPS+'wsp'))[ref['shape']])
    for node in dict.fromkeys(selected):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
            _prune_empty_drawing(parent)


def _prune_empty_drawing(node):
    """独立文本框移除后，空 drawing 不是有效的 Word 绘图节点。"""
    if node.tag == emit.W+'drawing' and not len(node):
        run = node.getparent()
        run.remove(node)
        if all(child.tag == emit.W+'rPr' for child in run):
            run.getparent().remove(run)


def remove_anchors(root, ids):
    for ident in set(ids):
        item = anchor(root, ident)
        parent = item.getparent()
        parent.remove(item)
        _prune_empty_drawing(parent)
