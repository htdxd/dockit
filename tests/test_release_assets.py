from pathlib import Path
from zipfile import ZipFile
import importlib.util
import json
import hashlib


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
