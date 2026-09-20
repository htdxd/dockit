import json

import pytest
from PIL import Image

from skill_toolbox.material_intake import prepare_images
from skill_toolbox.materials import MaterialService
from skill_toolbox.models import AssistantTurn, ConversationMessage, ImageContent, ToolCall
from skill_toolbox.runtime import AgentRuntime, TaskRequest


class Reader:
    def __init__(self):
        self.read = 0
        self.initial = None

    async def complete(self, system, messages, tools):
        if tools[0]['name'] == 'record_image':
            assert len(messages[0].images) == 1
            self.read += 1
            return AssistantTurn(tool_calls=[ToolCall(id='read', name='record_image', arguments={
                'kind': 'certificate', 'text': f'证书{self.read}：2024年，计算机等级考试合格。',
                'uncertainties': []})])
        self.initial = messages
        return AssistantTurn(tool_calls=[ToolCall(id='stop', name='task_failed', arguments={'reason':'test complete'})])


@pytest.mark.asyncio
async def test_all_images_are_transcribed_before_first_resume_turn(tmp_path, monkeypatch):
    from skill_toolbox.tools.resume_edit import ResumeEditService
    from skill_toolbox.contracts.common import OperationResult
    monkeypatch.setattr(ResumeEditService, 'prepare', lambda *a, **k: OperationResult(ok=True, data={'header_fields':['name']}))
    files = []
    for i in range(6):
        p = tmp_path / f'证书{i}.png'
        Image.new('RGB', (80, 80), (i * 20, 80, 90)).save(p)
        files.append(p)
    text = tmp_path / '经历.txt'
    text.write_text('独立开发工具，用户提供 Stars 120。', encoding='utf-8')
    provider = Reader()
    await AgentRuntime(provider, lambda e: None).run(TaskRequest(
        skill_id='resume_pro', user_prompt='手动填写：张三，求职开发工程师', output_dir=tmp_path/'out',
        materials=[*files, text], template_id='t001', tool_mode='domain',
        capabilities={'vision':True,'tool_calling':True}))
    assert provider.read == 6
    content = provider.initial[0].text
    assert 'Stars 120' in content and '手动填写：张三' in content
    assert all(f'证书{i}：2024年' in content for i in range(1,7))
    assert '"total": 7' in content and '"needs_review": []' in content
    assert content.count('"reading_status": "transcribed"') == 7
    assert not provider.initial[0].images


@pytest.mark.asyncio
async def test_unread_image_is_explicit_without_vision(tmp_path):
    p = tmp_path/'certificate.png'
    Image.new('RGB',(8,8),'white').save(p)
    service = MaterialService(tmp_path/'work')
    ir = service.prepare_material(p)
    provider = Reader()
    await prepare_images(service, provider, vision=False, emit=lambda e:None, debug=lambda e:None, timeout=5)
    assert provider.read == 0 and not ir.blocks
    assert '未识别' in ir.warnings[0]


def test_initial_user_images_reach_all_three_protocols():
    from skill_toolbox.providers.openai import OpenAIProvider
    from skill_toolbox.providers.openai_responses import OpenAIResponsesProvider
    from skill_toolbox.providers.anthropic import AnthropicProvider
    messages = [ConversationMessage(role='user', text='证书', images=[ImageContent(media_type='image/png',base64_data='TEST_IMAGE')])]
    for payload in (OpenAIProvider._messages('test',messages), OpenAIResponsesProvider._response_input(messages), AnthropicProvider._messages(messages)):
        assert 'TEST_IMAGE' in json.dumps(payload)
