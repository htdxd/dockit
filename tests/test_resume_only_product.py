from pathlib import Path
import pytest
from skill_toolbox.skills import load_skill
from skill_toolbox.llm_tools.resume_workflow import workflow_tools


@pytest.mark.parametrize('name',['docx_pro','ppt-master'])
def test_retired_features_cannot_be_loaded(name):
    with pytest.raises(ValueError,match='FEATURE_RETIRED'):
        load_skill(name)


def test_only_resume_generation_is_exposed():
    handlers = {tool['name'] for tool in workflow_tools()}
    assert not any(key.startswith(('docx_','ppt_')) for key in handlers)
    ui = Path('src/ui.ts').read_text(encoding='utf-8')
    assert 'id="page-ppt"' not in ui and 'id="page-docx"' not in ui
    assert '数据只在本地' not in ui
    assert 'DocKit Resume' in ui
