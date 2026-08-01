from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI

from skill_toolbox.models import (
    AssistantTurn,
    ConversationMessage,
    ProviderConfig,
    ToolCall,
)


class OpenAIProvider:
    def __init__(self, config: ProviderConfig) -> None:
        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "max_retries": 0,
            "timeout": 1200.0,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system_prompt: str,
        messages: Sequence[ConversationMessage],
        tools: list[dict[str, object]],
    ) -> AssistantTurn:
        api_messages = self._messages(system_prompt, messages)
        api_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in tools
        ]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            tools=api_tools,
            max_tokens=self.max_tokens,
        )
        message = response.choices[0].message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            raw_args = call.function.arguments
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                # Model sometimes emits malformed JSON for large content
                # (unescaped SVG XML). Try to recover path/content fields
                # so the tool can still execute instead of wasting a turn.
                arguments = self._recover_args(raw_args)
            calls.append(
                ToolCall(id=call.id, name=call.function.name, arguments=arguments)
            )
        text = message.content or ""
        # OpenAI rejects assistant messages where both content and tool_calls
        # are empty (400 "content or tool_calls must be set"). Supply a
        # placeholder so the next turn's request stays valid.
        if not text and not calls:
            text = "(no output)"
        return AssistantTurn(text=text, tool_calls=calls)

    @staticmethod
    def _recover_args(raw: str) -> dict[str, Any]:
        """Best-effort recovery of path/content from malformed JSON.

        When models emit large SVG content inside write tool calls, the JSON
        sometimes breaks on unescaped characters. We extract the 'path' field
        (short, usually intact) and the 'content' field (everything between
        the first "content": " and the final closing quote) so the write can
        proceed instead of wasting two turns on retry.
        """
        import re

        recovered: dict[str, Any] = {}
        # path is typically short and on the same line
        path_match = re.search(r'"path"\s*:\s*"([^"]+)"', raw)
        if path_match:
            recovered["path"] = path_match.group(1)
        # content: grab everything after "content": " up to the last "
        content_match = re.search(r'"content"\s*:\s*"', raw)
        if content_match:
            start = content_match.end()
            # take everything to the end, then strip trailing quote/brace
            tail = raw[start:]
            # remove trailing characters like " or } or whitespace
            tail = tail.rstrip()
            if tail.endswith('"}'):
                tail = tail[:-2]
            elif tail.endswith('"'):
                tail = tail[:-1]
            recovered["content"] = tail
        if not recovered:
            recovered["_invalid_json"] = raw[:500]
        return recovered

    @staticmethod
    def _messages(
        system_prompt: str, messages: Sequence[ConversationMessage]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            if message.role == "user":
                result.append({"role": "user", "content": message.text})
            elif message.role == "assistant":
                # OpenAI requires content or tool_calls on every assistant
                # message; never emit {"content": null} with no tool_calls.
                content = message.text or "(no output)" if not message.tool_calls else message.text or None
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                }
                if message.tool_calls:
                    assistant["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(
                                    call.arguments, ensure_ascii=False
                                ),
                            },
                        }
                        for call in message.tool_calls
                    ]
                result.append(assistant)
            else:
                for tool_result in message.tool_results:
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_result.tool_call_id,
                            "content": tool_result.content,
                        }
                    )
                    for image in tool_result.images:
                        result.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{image.media_type};base64,{image.base64_data}"
                                        },
                                    }
                                ],
                            }
                        )
        return result
