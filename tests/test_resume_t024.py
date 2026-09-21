"""t024 源映射、双栏边界与分发人像保护。"""
import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

from PIL import Image, ImageChops, ImageStat
from skill_toolbox.resume_layout import emit, t024


def test_t024_all_shapes_have_verified_page_positions():
    root = emit.load_document_xml(t024.TEMPLATE)
    anchors = list(root.iter(emit.WP + "anchor"))
    ids = {int(a.find(emit.WP + "docPr").get("id")) for a in anchors}
    assert ids == set(t024.SPEC["positions"])
    for anchor in anchors:
        ident = int(anchor.find(emit.WP + "docPr").get("id"))
        for axis, value in zip(("H", "V"), t024.PAGE_POSITIONS[ident]):
            position = anchor.find(emit.WP + "position" + axis)
            assert position.get("relativeFrom") == "page"
            assert abs(int(position[0].text) / emit.EMU - value) < 0.01


def test_t024_semantic_mapping_has_distinct_titles_and_bodies():
    root = emit.load_document_xml(t024.TEMPLATE)
    anchors = {int(a.find(emit.WP + "docPr").get("id")): a
               for a in root.iter(emit.WP + "anchor")}
    seen = set()
    for section in t024.SPEC["sections"].values():
        title = anchors[section["title_ids"][0]]
        body_id = section["body"]["anchor"]
        assert len(list(title.iter(emit.WPS + "wsp"))) == 3
        assert len(list(anchors[body_id].iter(emit.WPS + "wsp"))) == 1
        assert body_id not in seen
        seen.add(body_id)
    from skill_toolbox.resume_layout.component_template import source_key
    assert t024.SPEC['sections']['summary']['region'] == 'sidebar'
    assert t024.SPEC['sections'][source_key({'id': 'projects'}, t024.SPEC)]['region'] == 'main'
    left, right = (t024.SPEC["regions"][r] for r in ("sidebar", "main"))
    assert left["x_pt"] + left["width_pt"] < right["x_pt"]
    assert right["x_pt"] + right["width_pt"] < t024.PAGE_WIDTH_PT
    assert right["top_pt"] < t024.PAGE_POSITIONS[t024.PHOTO_ID][1]
    assert left["bottom_pt"] == right["bottom_pt"] < t024.PAGE_HEIGHT_PT


def test_t024_distributed_photo_is_existing_drawn_placeholder():
    from skill_toolbox.resume_layout.t109 import TEMPLATE as t109_template

    with ZipFile(t024.TEMPLATE) as package, ZipFile(t109_template) as reference:
        media = [n for n in package.namelist()
                 if n.startswith("word/media/") and not n.endswith("/")]
        assert media == [t024.PHOTO_PART]
        actual = Image.open(BytesIO(package.read(t024.PHOTO_PART))).convert("RGB")
        expected = Image.open(BytesIO(reference.read("word/media/image1.png"))).convert("RGB")
        assert actual.size == expected.size
        # JPEG re-encoding changes individual values, but cannot hide a different image.
        assert max(ImageStat.Stat(ImageChops.difference(actual, expected)).mean) < 2
        root = emit.etree.fromstring(package.read("word/document.xml"))
        assert all(not node.get("descr") for node in root.iter(emit.WP + "docPr"))
    manifest = json.loads(t024.TEMPLATE.with_name("manifest.json").read_text(encoding="utf-8"))
    assert manifest["template_sha256"] == hashlib.sha256(t024.TEMPLATE.read_bytes()).hexdigest()
