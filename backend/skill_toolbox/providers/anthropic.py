from __future__ import annotations

import random
import string
from collections.abc import Sequence
from typing import Any

from anthropic import AsyncAnthropic

from skill_toolbox.capabilities import anthropic_reasoning_budget
from skill_toolbox.models import (
    AssistantTurn,
    ConversationMessage,
    ProviderConfig,
    ReasoningLevel,
    ToolCall,
)
from skill_toolbox.providers.probes import classify_probe_error, run_probes


def _thinking_kwargs(level: ReasoningLevel, max_tokens: int) -> dict[str, Any]:
    """Map a user reasoning level to Anthropic extended-thinking parameters.

    ``thinking={"type": "enabled", "budget_tokens": N}`` where N must be in
    [1024, max_tokens) — Anthropic enforces both bounds (>= 1024 and <
    max_tokens) and requires temperature=1 when thinking is enabled. ``auto``
    sends nothing (provider default behaviour). If the target budget collides
    with the max_tokens cap (e.g. a small max_tokens), fall back to auto rather
    than constructing a request that is guaranteed to 400.
    """
    if level == "none":
        return {"thinking": {"type": "disabled"}}
    budget = anthropic_reasoning_budget(level)
    if not budget:
        return {}
    budget = max(budget, 1024)
    if budget >= max_tokens:
        return {}  # 无法满足 budget ∈ [1024, max_tokens) → 省略参数，用默认行为
    return {"thinking": {"type": "enabled", "budget_tokens": budget}, "temperature": 1}


class AnthropicProvider:
    def __init__(self, config: ProviderConfig) -> None:
        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "max_retries": 0,
            "timeout": 1200.0,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = AsyncAnthropic(**kwargs)
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.reasoning_level = config.reasoning_level

    async def complete(
        self,
        system_prompt: str,
        messages: Sequence[ConversationMessage],
        tools: list[dict[str, object]],
    ) -> AssistantTurn:
        self.client.max_retries = 3
        params: dict[str, Any] = {
            "model": self.model,
            "system": system_prompt,
            "messages": self._messages(messages),
            "tools": tools,
            "max_tokens": self.max_tokens,
        }
        params.update(_thinking_kwargs(self.reasoning_level, self.max_tokens))
        response = await self.client.messages.create(**params)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        # thinking blocks are deliberately skipped: we never surface raw CoT to
        # the agent, the UI, or the JSONL logs.
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

    async def probe(
        self,
        capabilities: list[str] | None = None,
    ) -> dict[str, tuple[str, str | None]]:
        """Deterministic capability probe against the Anthropic Messages API.

        Same contract as the OpenAI provider: one minimal nonce-anchored request
        per capability. reasoning_control is verified only when the response
        carries thinking metadata / usage (thinking_blocks or output_tokens
        beyond the text), and its ``control`` type is "budget".
        """
        self.client.max_retries = 0
        return await run_probes(capabilities, {
            "tool_calling": self._probe_tool_calling,
            "vision": self._probe_vision,
            "reasoning_control": self._probe_reasoning_control,
        })

    @staticmethod
    def _nonce(length: int = 6) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

    async def _probe_tool_calling(self) -> tuple[str, str | None]:
        nonce = self._nonce()
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"调用 echo 工具，参数 value 必须原样返回此短码：{nonce}。"
                            "不要输出任何其他文字。"
                        ),
                    }
                ],
                tools=[
                    {
                        "name": "echo",
                        "description": "Returns the given value unchanged",
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001
            return self._classify_error("tool_calling", exc)
        calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not calls:
            return ("unknown", "no tool call returned")
        call = calls[0]
        if getattr(call, "name", "") != "echo":
            return ("unknown", f"wrong tool: {getattr(call, 'name', '')}")
        args = dict(getattr(call, "input", {}) or {})
        if args.get("value") != nonce:
            return ("unknown", "nonce mismatch")
        return ("verified", None)

    async def _probe_vision(self) -> tuple[str, str | None]:
        code = self._nonce(4).upper()
        try:
            import io as _io

            from PIL import Image, ImageDraw
        except Exception:  # noqa: BLE001
            return ("probe_error", "pillow unavailable")
        img = Image.new("RGB", (64, 64), "white")
        ImageDraw.Draw(img).text((6, 24), code, fill="black")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        import base64
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=32,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "图片里显示的 4 位大写短码是什么？只输出短码本身。",
                            },
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(data).decode(),
                                },
                            },
                        ],
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001
            return self._classify_error("vision", exc)
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        normalized = "".join(ch for ch in text.upper() if ch.isalnum())
        if normalized == code:
            return ("verified", None)
        return ("unknown", "recognised text did not match")

    async def _probe_reasoning_control(self) -> tuple[str, str | None]:
        budget = 1024
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                temperature=1,
                thinking={"type": "enabled", "budget_tokens": budget},
                messages=[{"role": "user", "content": "1+1=?"}],
            )
        except Exception as exc:  # noqa: BLE001
            return self._classify_error("reasoning_control", exc)
        has_thinking = any(
            getattr(b, "type", "") == "thinking" for b in response.content
        )
        if has_thinking:
            return ("verified", "budget")
        # Some gateways 200 but strip thinking blocks; keep unknown (not a lie).
        return ("unknown", "no thinking block returned")

    _classify_error = staticmethod(classify_probe_error)
