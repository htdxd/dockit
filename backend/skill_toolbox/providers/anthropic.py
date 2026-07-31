from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from anthropic import AsyncAnthropic

from skill_toolbox.models import (
    AssistantTurn,
    ConversationMessage,
    ProviderConfig,
    ToolCall,
)


class AnthropicProvider:
    def __init__(self, config: ProviderConfig) -> None:
        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "max_retries": 0,
            "timeout": 90.0,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = AsyncAnthropic(**kwargs)
        self.model = config.model
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system_prompt: str,
        messages: Sequence[ConversationMessage],
        tools: list[dict[str, object]],
    ) -> AssistantTurn:
        response = await self.client.messages.create(
            model=self.model,
            system=system_prompt,
            messages=self._messages(messages),
            tools=tools,
            max_tokens=self.max_tokens,
        )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )
        return AssistantTurn(text="\n".join(text_parts), tool_calls=calls)

    @staticmethod
    def _messages(messages: Sequence[ConversationMessage]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "user":
                result.append({"role": "user", "content": message.text})
            elif message.role == "assistant":
                content: list[dict[str, Any]] = []
                if message.text:
                    content.append({"type": "text", "text": message.text})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                result.append({"role": "assistant", "content": content})
            else:
                content = []
                for tool_result in message.tool_results:
                    parts: list[dict[str, Any]] = [
                        {"type": "text", "text": tool_result.content}
                    ]
                    parts.extend(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": image.media_type,
                                "data": image.base64_data,
                            },
                        }
                        for image in tool_result.images
                    )
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_result.tool_call_id,
                            "is_error": not tool_result.success,
                            "content": parts,
                        }
                    )
                result.append({"role": "user", "content": content})
        return result
