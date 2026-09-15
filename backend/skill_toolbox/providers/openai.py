from __future__ import annotations

import base64
import json
import random
import string
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI

from skill_toolbox.capabilities import load_probe_report, openai_reasoning_effort
from skill_toolbox.models import (
    AssistantTurn,
    ConversationMessage,
    ProviderConfig,
    ReasoningLevel,
    ToolCall,
)
from skill_toolbox.providers.probes import classify_probe_error, run_probes


def _reasoning_kwargs(level: ReasoningLevel) -> dict[str, Any]:
    """Map a user reasoning level to OpenAI-native request parameters.

    OpenAI reasoning models (o1/o3/gpt-5) accept ``reasoning_effort`` with a
    closed enum. ``auto`` sends nothing (provider default); explicit levels are
    passed through so the provider validates model-specific support.
    ``max_tokens`` stays the standard chat-completions parameter for
    non-reasoning models. Responses uses its own provider implementation.
    """
    effort = openai_reasoning_effort(level)
    if not effort:
        return {}
    return {"reasoning_effort": effort}


def _probe_reasoning_max_tokens(model: str) -> int:
    """Return a completion cap compatible with an effort-based probe.

    Qwen gateways may map ``reasoning_effort=low`` to an 8192-token thinking
    budget and require the completion cap to be greater than that budget.
    Other OpenAI-compatible providers can use a smaller probe cap.
    """
    return 16384 if "qwen" in (model or "").lower() else 1024


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
        self.reasoning_level = config.reasoning_level
        self.tool_choice = "required" if load_probe_report(config).forced_tool_calling.status == "verified" else "auto"

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
        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "tools": api_tools,
            "max_tokens": self.max_tokens,
        }
        params.update(_reasoning_kwargs(self.reasoning_level))
        if api_tools:
            params["tool_choice"] = self.tool_choice
        response = await self.client.chat.completions.create(**params)
        choice = response.choices[0]
        message = choice.message
        finish_reason = getattr(choice, "finish_reason", None)
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as exc:
                arguments = {"_json_error": {"message": exc.msg, "position": exc.pos,
                                              "length": len(call.function.arguments)},
                             "_invalid_json": call.function.arguments[:2000]}
                if isinstance(finish_reason, str):
                    arguments["_finish_reason"] = finish_reason
            calls.append(
                ToolCall(id=call.id, name=call.function.name, arguments=arguments)
            )
        text = message.content or ""
        # OpenAI rejects assistant messages where both content and tool_calls
        # are empty (400). Supply a placeholder so the next request stays valid.
        if not text and not calls:
            text = "(no output)"
        metadata: dict[str, Any] = {}
        if isinstance(finish_reason, str):
            metadata["finish_reason"] = finish_reason
        usage = getattr(response, "usage", None)
        counts = {
            name: getattr(usage, name, None)
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        counts["reasoning_tokens"] = getattr(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
        )
        counts["cached_tokens"] = getattr(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
        )
        counts = {name: value for name, value in counts.items() if type(value) is int}
        if counts:
            metadata["usage"] = counts
        return AssistantTurn(text=text, tool_calls=calls, response_metadata=metadata)

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
                if not message.tool_calls and not message.text:
                    assistant["content"] = "(no output)"
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
                # 同批工具必须全部回复后才能插入用户图片消息。
                for tool_result in message.tool_results:
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

    async def probe(
        self,
        capabilities: list[str] | None = None,
    ) -> dict[str, tuple[str, str | None]]:
        """Deterministic capability probe against the OpenAI / compatible API.

        Each probe is one small, minimal, nonce-anchored request:
        - tool_calling: unique echo(value) tool, model must call it with the
          nonce → verified.
        - vision: in-memory generated image with a random short code, model must
          return the code → verified (recognition errors keep unknown, so we
          never mistake vision quality for protocol support).
        - reasoning_control: minimal request with reasoning_effort=low; verified
          only when the response carries reasoning_effort evidence. A plain HTTP
          200 gateway without verifiable metadata stays unknown.
        No raw CoT, no API keys, no user materials are ever returned.
        """
        return await run_probes(capabilities, {
            "tool_calling": self._probe_tool_calling,
            "vision": self._probe_vision,
            "reasoning_control": self._probe_reasoning_control,
            "forced_tool_calling": lambda: self._probe_tool_calling(force=True),
        })

    @staticmethod
    def _nonce(length: int = 6) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

    async def _probe_tool_calling(self, force: bool = False) -> tuple[str, str | None]:
        nonce = self._nonce()
        options = {"tool_choice": "required", **_reasoning_kwargs(self.reasoning_level)} if force else {}
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
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
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "description": "Returns the given value unchanged",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        },
                    }
                ],
                # reasoning 模型（o1/o3/gpt-5）的 chat.completions 弃用 max_tokens、
                # 改用 max_completion_tokens；非 reasoning 模型两者皆可。探测统一
                # 用 max_completion_tokens，避免对 gpt-5/o3 误报「不支持工具调用」。
                max_completion_tokens=512,
                **options,
            )
        except Exception as exc:  # noqa: BLE001
            return self._classify_error("forced_tool_calling" if force else "tool_calling", exc)
        message = response.choices[0].message
        calls = message.tool_calls or []
        if not calls:
            return ("unknown", "no tool call returned")
        call = calls[0]
        if call.function.name != "echo":
            return ("unknown", f"wrong tool: {call.function.name}")
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            return ("unknown", "malformed tool arguments")
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
        small = Image.new("RGB", (64, 64), "white")
        ImageDraw.Draw(small).text((6, 24), code, fill="black")
        img = small.resize((256, 256), Image.Resampling.NEAREST)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "图片里显示的 4 位大写短码是什么？只输出短码本身。",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{base64.b64encode(data).decode()}",
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
                max_completion_tokens=1024,
            )
        except Exception as exc:  # noqa: BLE001
            return self._classify_error("vision", exc)
        text = (response.choices[0].message.content or "").strip()
        normalized = "".join(ch for ch in text.upper() if ch.isalnum())
        if normalized == code:
            return ("verified", None)
        return ("unknown", "recognised text did not match")

    async def _probe_reasoning_control(self) -> tuple[str, str | None]:
        max_completion_tokens = _probe_reasoning_max_tokens(self.model)
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "1+1=?"}],
                # reasoning 模型（o1/o3/gpt-5 等）的 chat.completions 接口弃用
                # max_tokens、改用 max_completion_tokens；探测只发它，避免 400。
                max_completion_tokens=max_completion_tokens,
                reasoning_effort="low",
            )
        except Exception as exc:  # noqa: BLE001
            return self._classify_error("reasoning_control", exc)
        usage = response.usage
        # Evidence = the vendor echoed our reasoning_effort or reported
        # reasoning tokens. A plain 200 from a gateway without metadata is
        # deliberately NOT verified (kept unknown).
        effort_echo = getattr(response, "reasoning_effort", None)
        reasoning_tokens = (usage.completion_tokens_details.reasoning_tokens if usage and usage.completion_tokens_details else 0) or 0
        if effort_echo is not None or reasoning_tokens > 0:
            return ("verified", "effort")
        return ("unknown", "no reasoning metadata returned")

    _classify_error = staticmethod(classify_probe_error)
