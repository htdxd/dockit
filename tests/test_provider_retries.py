"""通过 SDK 的真实 HTTP 路径校验重试边界，而不是仅检查配置数字。"""
import asyncio
import httpx
import pytest
from skill_toolbox.models import ProviderConfig, ConversationMessage
from skill_toolbox.providers import create_provider
from skill_toolbox.providers import probes


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'openai_responses', 'anthropic'])
@pytest.mark.parametrize('status,expected', [(503, 4), (429, 4), (403, 1), (400, 1)])
async def test_model_retry_limit_and_nonretryable_failures(kind, status, expected):
    provider = create_provider(ProviderConfig(kind=kind, model='test', api_key='test-key'))
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'type': 'api_error', 'message': 'test failure'}},
                              headers={'retry-after-ms': '1'})
    await provider.client.close()
    from openai import AsyncOpenAI
    from anthropic import AsyncAnthropic
    sdk = AsyncAnthropic if kind == 'anthropic' else AsyncOpenAI
    provider.client = sdk(api_key='test-key', max_retries=0, timeout=1200.0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        with pytest.raises(Exception) as failure:
            await provider.complete('test', [ConversationMessage(role='user', text='hello')], [])
        assert getattr(failure.value, 'status_code', None) == status
        assert len(requests) == expected
    finally:
        await provider.client.close()


@pytest.mark.asyncio
async def test_slow_probe_does_not_discard_other_results(monkeypatch):
    monkeypatch.setattr(probes, 'PROBE_ITEM_TIMEOUT', 0.01)
    async def slow():
        await asyncio.sleep(1)
    async def good():
        return ('verified', None)
    results = await probes.run_probes(None, {'tool_calling': good, 'vision': slow, 'reasoning_control': good})
    assert results['tool_calling'][0] == results['reasoning_control'][0] == 'verified'
    assert results['vision'][0] == 'probe_error'
