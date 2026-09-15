"""紧凑排版改变纵向度量，不裁剪事实或沿用窄组件宽度。"""
import copy

import pytest

from skill_toolbox.resume_layout.typography import apply_density, line_pitch


@pytest.mark.parametrize("template", ["t001", "t109"])
def test_compact_pitch_matches_effective_font_and_keeps_original_content(template):
    content = {"header": {"photo_path": "original.png"}, "sections": [
        {"key": "projects", "scale": 0.9, "entries": [
            {"id": "a", "font_size_pt": 10, "scale": 1, "lines": ["完整技术细节，不得删减。"]}]}]}
    old = copy.deepcopy(content)
    apply_density(content, "compact", template)
    section = content["sections"][0]
    entry = section["entries"][0]
    assert entry["font_size_pt"] == 9 and entry["scale"] == section["scale"] == 1
    assert content["header"] == old["header"]
    assert entry["lines"] == old["sections"][0]["entries"][0]["lines"]
    assert line_pitch(entry, {**section, "density": "compact"}, template) == 13.5
    assert line_pitch(entry, section, template) > 13.5
