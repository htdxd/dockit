import json
from pathlib import Path
import re

import pytest
from skill_toolbox.models import AssistantTurn
from skill_toolbox.providers.mock import ScriptedProvider
from skill_toolbox.runtime import AgentRuntime, TaskRequest
from skill_toolbox.resume_layout.profiles import SUPPORTED_TEMPLATES

ROOT = Path(__file__).resolve().parents[1]


def test_development_does_not_depend_on_bundled_runtime():
    development = json.loads((ROOT / 'src-tauri/tauri.conf.json').read_text(encoding='utf-8'))
    release = json.loads((ROOT / 'src-tauri/tauri.bundle.conf.json').read_text(encoding='utf-8'))
    scripts = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['scripts']
    assert not development['bundle'].get('resources')
    assert release['bundle']['resources'] == {'../backend-dist/desktop/': 'runtime/'}
    assert '--config src-tauri/tauri.bundle.conf.json' in scripts['package']


def test_artifact_opener_allows_user_selected_paths_only_with_default_app():
    permissions = json.loads((ROOT / 'src-tauri/capabilities/default.json').read_text())['permissions']
    opener = next(p for p in permissions if isinstance(p, dict) and p['identifier'] == 'opener:allow-open-path')
    assert opener['allow'] == [{'path': '**', 'app': None}]


def test_ui_only_offers_component_templates():
    ui = (ROOT / 'src/ui.ts').read_text(encoding='utf-8')
    assert set(re.findall(r'data-template="([^"]+)"', ui)) == SUPPORTED_TEMPLATES


@pytest.mark.asyncio
@pytest.mark.parametrize('template', ['t002', 't026', 't046'])
async def test_unadapted_template_rejected_before_model_call(tmp_path, template):
    result = await AgentRuntime(ScriptedProvider([AssistantTurn()]), lambda _: None).run(TaskRequest(
        skill_id='resume_pro', template_id=template, user_prompt='生成简历', output_dir=tmp_path))
    assert result.status == 'failed' and '已组件化' in result.error
