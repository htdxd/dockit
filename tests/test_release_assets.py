import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def test_only_sanitized_templates_are_distributed():
    spec = importlib.util.spec_from_file_location('sanitize_templates', ROOT/'scripts/sanitize_release_templates.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = ROOT/'backend/skill_toolbox/skill_defs/resume_pro/templates'
    from skill_toolbox.resume_layout.profiles import SUPPORTED_TEMPLATES, get_profile
    assert {p.parent.name for p in folder.glob('*/template.docx')} == SUPPORTED_TEMPLATES
    for template in SUPPORTED_TEMPLATES:
        part = get_profile(template).photo_part
        path = folder/template/'template.docx'
        with ZipFile(path) as package:
            assert package.read(part) in (module.placeholder('JPEG'), module.placeholder('PNG'))
            media = [n for n in package.namelist() if n.startswith('word/media/') and not n.endswith('/')]
            assert part in media
        manifest = json.loads((folder/template/'manifest.json').read_text(encoding='utf-8'))
        assert manifest['template_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_uses_agpl_and_exposes_source_link():
    assert 'GNU AFFERO GENERAL PUBLIC LICENSE' in (ROOT/'LICENSE').read_text(encoding='utf-8')
    assert 'license = "AGPL-3.0-only"' in (ROOT/'pyproject.toml').read_text(encoding='utf-8')
    for name in ('src/ui.ts','src/web.ts'):
        assert 'https://github.com/htdxd/dockit' in (ROOT/name).read_text(encoding='utf-8')


def test_changed_template_is_rejected_before_measurement(tmp_path):
    import pytest
    from skill_toolbox.contracts.common import ToolError
    from skill_toolbox.tools.resume_edit import ResumeEditService
    folder = tmp_path / 'templates/t001'
    folder.mkdir(parents=True)
    (folder / 'template.docx').write_bytes(b'changed original')
    (folder / 'manifest.json').write_text(json.dumps({'template_sha256': '0' * 64}), encoding='utf-8')
    service = ResumeEditService(tmp_path, folder.parent, template_id='t001')
    with pytest.raises(ToolError) as error:
        service._template_dir('t001')
    assert error.value.code == 'TEMPLATE_CHANGED'
