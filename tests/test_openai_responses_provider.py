import json

import httpx
import pytest
from openai import AsyncOpenAI

from skill_toolbox.models import ConversationMessage, ImageContent, ProviderConfig, ToolResult
from skill_toolbox.providers import create_provider
from skill_toolbox.providers import openai as chat_module
from skill_toolbox.providers.openai import OpenAIProvider
from skill_toolbox.providers.openai_responses import OpenAIResponsesProvider


def response(output, *, status="completed", incomplete=None):
    return {"id": "resp_test", "object": "response", "created_at": 1, "model": "test-model",
            "status": status, "error": None, "incomplete_details": incomplete, "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                      "input_tokens_details": {"cached_tokens": 50},
                      "output_tokens_details": {"reasoning_tokens": 10}}}


def message(text):
    return {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


@pytest.mark.asyncio
async def test_responses_transport_replays_reasoning_tools_and_images_without_logging_state(monkeypatch):
    requests = []

    def handler(request):
        assert request.url.path == "/v1/responses"
        requests.append(json.loads(request.content))
        items = [
            {"id": "rs_test", "type": "reasoning", "summary": [], "encrypted_content": "opaque-reasoning"},
            {"id": "fc_test", "type": "function_call", "call_id": "call_test", "name": "echo",
             "arguments": '{"value":"ok"}', "status": "completed"},
        ] if len(requests) == 1 else [message("done")]
        return httpx.Response(200, json=response(items))

    async with AsyncOpenAI(api_key="test-key", base_url="https://test.invalid/v1",
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        monkeypatch.setattr(chat_module, "AsyncOpenAI", lambda **kwargs: client)
        provider = create_provider(ProviderConfig(kind="openai_responses", model="test-model", api_key="test-key", reasoning_level="balanced"))
        assert type(provider) is OpenAIResponsesProvider
        assert type(create_provider(ProviderConfig(kind="openai", model="test-model", api_key="test-key"))) is OpenAIProvider
        history = [ConversationMessage(role="user", text="test")]
        tools = [{"name": "echo", "description": "echo", "input_schema": {"type": "object", "properties": {}}}]
        first = await provider.complete("system", history, tools)
        assert first.tool_calls[0].id == "call_test"
        assert first.response_metadata["usage"]["cached_tokens"] == 50
        assert "opaque-reasoning" not in first.model_dump_json()
        history += [ConversationMessage(role="assistant", text=first.text, tool_calls=first.tool_calls,
                                        provider_items=first.provider_items),
                    ConversationMessage(role="tool", tool_results=[ToolResult(
                        name="echo", tool_call_id="call_test", success=True, content="ok",
                        images=[ImageContent(media_type="image/png", base64_data="test-image")])])]
        second = await provider.complete("system", history, tools)
        assert second.text == "done"
        assert requests[0]["reasoning"] == {"effort": "medium"}
        assert requests[0]["tool_choice"] == "auto"
        assert requests[0]["store"] is False and requests[0]["max_output_tokens"] == 65536
        assert requests[0]["tools"][0]["strict"] is False
        assert "function" not in requests[0]["tools"][0] and "messages" not in requests[0]
        items = requests[1]["input"]
        assert items[1]["encrypted_content"] == "opaque-reasoning"
        assert items[2]["call_id"] == items[3]["call_id"] == "call_test"
        assert items[3]["type"] == "function_call_output" and items[3]["output"] == "ok"
        assert items[4]["content"][0] == {"type": "input_image", "image_url": "data:image/png;base64,test-image", "detail": "high"}


@pytest.mark.asyncio
async def test_responses_probes_use_responses_endpoint(monkeypatch):
    requests = []

    def handler(request):
        assert request.url.path == "/v1/responses"
        data = json.loads(request.content)
        requests.append(data)
        output = [{"id": "fc", "type": "function_call", "call_id": "call", "name": "echo", "arguments": '{"value":"ABCD"}'}] if data.get("tools") else [message("ABCD")]
        payload = response(output)
        if data.get("reasoning"):
            payload["reasoning"] = {"effort": "low"}
        return httpx.Response(200, json=payload)

    async with AsyncOpenAI(api_key="test-key", base_url="https://test.invalid/v1",
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        monkeypatch.setattr(chat_module, "AsyncOpenAI", lambda **kwargs: client)
        provider = OpenAIResponsesProvider(ProviderConfig(kind="openai_responses", model="test-model", api_key="test-key"))
        monkeypatch.setattr(provider, "_nonce", lambda length=6: "ABCD")
        result = await provider.probe()
        assert all(status == "verified" for status, detail in result.values())
        assert len(requests) == 4 and requests[2]["reasoning"] == {"effort": "low"}
        assert requests[-1]["tool_choice"] == "required"
        assert requests[1]["input"][0]["content"][1]["type"] == "input_image"


@pytest.mark.asyncio
async def test_incomplete_arguments_preserve_length_diagnostic(monkeypatch):
    payload = response([{"id": "fc", "type": "function_call", "call_id": "call", "name": "echo", "arguments": '{"value":'}],
                       status="incomplete", incomplete={"reason": "max_output_tokens"})
    async with AsyncOpenAI(api_key="test-key", base_url="https://test.invalid/v1",
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))) as client:
        monkeypatch.setattr(chat_module, "AsyncOpenAI", lambda **kwargs: client)
        turn = await OpenAIResponsesProvider(ProviderConfig(kind="openai_responses", model="test-model", api_key="test-key")).complete("system", [], [])
        assert turn.response_metadata["finish_reason"] == "length"
        assert turn.tool_calls[0].arguments["_finish_reason"] == "length"
        assert "_invalid_json" in turn.tool_calls[0].arguments
