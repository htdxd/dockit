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
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                arguments = {"_invalid_json": call.function.arguments}
            calls.append(
                ToolCall(id=call.id, name=call.function.name, arguments=arguments)
            )
        return AssistantTurn(text=message.content or "", tool_calls=calls)

    @staticmethod
    def _messages(
        system_prompt: str, messages: Sequence[ConversationMessage]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            if message.role == "user":
                result.append({"role": "user", "content": message.text})
            elif message.role == "assistant":
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.text or None,
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
