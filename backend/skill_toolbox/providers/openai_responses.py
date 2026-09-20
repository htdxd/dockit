"""Responses 协议：独立端点、工具结果和图片格式，支持无服务端存储的多轮调用。"""
import base64
import io
import json

from skill_toolbox.capabilities import openai_reasoning_effort
from skill_toolbox.models import AssistantTurn, ToolCall
from skill_toolbox.providers.openai import OpenAIProvider, _probe_reasoning_max_tokens


class OpenAIResponsesProvider(OpenAIProvider):
    async def _request(self, **parameters):
        return await self.client.responses.create(
            model=self.model, store=False, include=["reasoning.encrypted_content"], **parameters)

    @staticmethod
    def _response_input(messages):
        items = []
        for message in messages:
            if message.role == "user":
                content = ([{"type": "input_text", "text": message.text}] + [
                    {"type": "input_image", "image_url": f"data:{im.media_type};base64,{im.base64_data}", "detail": "high"}
                    for im in message.images]) if message.images else message.text
                items.append({"role": "user", "content": content})
            elif message.role == "assistant":
                if message.provider_items:
                    retained = {call.id for call in message.tool_calls}
                    items.extend(item for item in message.provider_items
                                 if item.get("type") != "function_call" or item.get("call_id") in retained)
                else:
                    if message.text:
                        items.append({"role": "assistant", "content": message.text})
                    items.extend({"type": "function_call", "call_id": call.id, "name": call.name,
                                  "arguments": json.dumps(call.arguments, ensure_ascii=False)}
                                 for call in message.tool_calls)
            else:
                for result in message.tool_results:
                    items.append({"type": "function_call_output", "call_id": result.tool_call_id,
                                  "output": result.content})
                for result in message.tool_results:
                    if result.images:
                        items.append({"role": "user", "content": [
                            {"type": "input_image", "image_url": f"data:{image.media_type};base64,{image.base64_data}",
                             "detail": "high"} for image in result.images]})
        return items

    async def complete(self, system_prompt, messages, tools):
        self.client.max_retries = 3
        parameters = {
            "instructions": system_prompt, "input": self._response_input(messages),
            "max_output_tokens": self.max_tokens,
            "tools": [{"type": "function", "name": tool["name"], "description": tool["description"],
                       "parameters": tool["input_schema"], "strict": False} for tool in tools],
        }
        effort = openai_reasoning_effort(self.reasoning_level)
        if tools:
            parameters["tool_choice"] = self.tool_choice
        if effort:
            parameters["reasoning"] = {"effort": effort}
        response = await self._request(**parameters)
        if response.status == "failed":
            raise RuntimeError(getattr(response.error, "message", None) or "Responses request failed")
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        finish = "length" if reason == "max_output_tokens" else reason or response.status
        calls = []
        for item in response.output:
            if item.type != "function_call":
                continue
            try:
                arguments = json.loads(item.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须为 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                arguments = {"_json_error": {"message": str(exc), "position": getattr(exc, "pos", 0),
                                              "length": len(item.arguments)},
                             "_invalid_json": item.arguments[:2000], "_finish_reason": finish}
            calls.append(ToolCall(id=item.call_id, name=item.name, arguments=arguments))
        usage = response.usage
        counts = {"prompt_tokens": getattr(usage, "input_tokens", None),
                  "completion_tokens": getattr(usage, "output_tokens", None),
                  "total_tokens": getattr(usage, "total_tokens", None),
                  "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None),
                  "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None)}
        text = response.output_text or "\n".join(
            part.refusal for item in response.output if item.type == "message"
            for part in item.content if part.type == "refusal")
        return AssistantTurn(
            text=text or ("" if calls else "(no output)"), tool_calls=calls,
            response_metadata={"finish_reason": "tool_calls" if calls and response.status == "completed" else finish,
                               "usage": {key: value for key, value in counts.items() if type(value) is int}},
            provider_items=[item.model_dump(exclude_none=True) for item in response.output],
        )

    async def _probe_tool_calling(self, force=False):
        nonce = self._nonce()
        options = {"tool_choice": "required"} if force else {}
        effort = openai_reasoning_effort(self.reasoning_level)
        if force and effort:
            options["reasoning"] = {"effort": effort}
        try:
            response = await self._request(
                input=f"调用 echo 工具，value 必须原样返回短码 {nonce}，不要输出其他文字。", max_output_tokens=1024,
                tools=[{"type": "function", "name": "echo", "description": "Return the value",
                        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                       "required": ["value"]}, "strict": False}], **options)
            call = next((item for item in response.output if item.type == "function_call"), None)
            if call and call.name == "echo" and json.loads(call.arguments).get("value") == nonce:
                return ("verified", None)
            return ("unknown", "no matching tool call returned")
        except Exception as exc:
            return self._classify_error("tool_calling", exc)

    async def _probe_vision(self):
        from PIL import Image, ImageDraw

        code = self._nonce(4).upper()
        image = Image.new("RGB", (64, 64), "white")
        ImageDraw.Draw(image).text((6, 24), code, fill="black")
        buffer = io.BytesIO()
        image.resize((256, 256), Image.Resampling.NEAREST).save(buffer, format="PNG")
        try:
            response = await self._request(max_output_tokens=1024, input=[{"role": "user", "content": [
                {"type": "input_text", "text": "图片里显示的4位大写短码是什么？只输出短码。"},
                {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
                 "detail": "high"}]}])
            actual = "".join(char for char in response.output_text.upper() if char.isalnum())
            return ("verified", None) if actual == code else ("unknown", "recognised text did not match")
        except Exception as exc:
            return self._classify_error("vision", exc)

    async def _probe_reasoning_control(self):
        try:
            response = await self._request(input="1+1=?", max_output_tokens=_probe_reasoning_max_tokens(self.model),
                                           reasoning={"effort": "low"})
            effort = getattr(getattr(response, "reasoning", None), "effort", None)
            tokens = getattr(getattr(response.usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
            if effort == "low" or (effort is None and tokens > 0):
                return ("verified", "effort")
            return ("unknown", "no reasoning metadata returned")
        except Exception as exc:
            return self._classify_error("reasoning_control", exc)
