"""模型回合与工具执行。业务准备、上下文裁剪和交付由调用方提供。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import ValidationError

from skill_toolbox.contracts.common import ToolError
from skill_toolbox.models import ConversationMessage, ToolResult
from skill_toolbox.runtime_types import TaskResult
from skill_toolbox.unicode_utils import redact_secrets


def redact_debug(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_debug(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_debug(item) for item in value]
    return value


def truncate(value, limit=2000):
    text = redact_secrets(json.dumps(value, ensure_ascii=False, default=str))
    return (
        text if len(text) <= limit else text[:limit] + f"…<+{len(text) - limit} bytes>"
    )


def error_result(call, code, message, **details):
    return ToolResult(
        tool_call_id=call.id,
        name=call.name,
        success=False,
        content=json.dumps(
            {"code": code, "message": redact_secrets(message), **details},
            ensure_ascii=False,
        ),
        metadata={"error_code": code, "message": redact_secrets(message)},
    )


@dataclass(frozen=True)
class ToolDefinition:
    spec: dict
    execute: Callable[[dict], Awaitable[ToolResult | TaskResult]]
    timeout: float | None = 90.0


async def execute_tool(call, definitions, *, emit, debug, cancel):
    emit({"type": "tool_started", "tool": call.name})
    debug(
        {
            "phase": "tool_call",
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": truncate(call.arguments),
        }
    )
    definition = definitions.get(call.name)
    try:
        if definition is None:
            raise ToolError("TOOL_NOT_AVAILABLE", f"当前任务未开放工具 {call.name}。")
        if "_invalid_json" in call.arguments:
            truncated = call.arguments.get("_finish_reason") == "length"
            raise ToolError(
                "MODEL_OUTPUT_TRUNCATED" if truncated else "TOOL_JSON_INVALID",
                "工具参数被截断或不是完整 JSON。请完整重发，必要时拆分编辑操作。",
                retryable=True,
            )
        result = await asyncio.wait_for(
            definition.execute(call.arguments), timeout=definition.timeout
        )
    except TimeoutError:
        cancel(False)
        result = error_result(
            call, "TOOL_TIMEOUT", f"Tool timed out after {definition.timeout:g} seconds"
        )
        emit(
            {
                "type": "tool_timeout",
                "tool": call.name,
                "timeout_seconds": definition.timeout,
            }
        )
    except asyncio.CancelledError:
        cancel(True)
        raise
    except ToolError as exc:
        result = error_result(
            call, exc.code, str(exc), retryable=exc.retryable, suggestion=exc.suggestion
        )
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(
                include_input=False, include_context=False, include_url=False
            )
        )
        result = error_result(
            call,
            "TOOL_ARGUMENTS_INVALID",
            detail,
            suggestion="按工具 schema 修正参数，不要原样重试错误参数。",
        )
    except (KeyError, TypeError, ValueError) as exc:
        result = error_result(call, "TOOL_ARGUMENTS_INVALID", str(exc))
    except Exception as exc:
        result = error_result(call, "TOOL_FAILED", str(exc))
    if isinstance(result, ToolResult):
        result = result.model_copy(update={"tool_call_id": call.id, "name": call.name})
    success = isinstance(result, TaskResult) or result.success
    emit({"type": "tool_finished", "tool": call.name, "success": success})
    debug(
        {
            "phase": "tool_result",
            "tool_call_id": call.id,
            "tool": call.name,
            "success": success,
            "content": truncate(result.content, 4000)
            if isinstance(result, ToolResult)
            else result.status,
            "image_count": len(result.images) if isinstance(result, ToolResult) else 0,
            "summary": result.metadata if isinstance(result, ToolResult) else {},
        }
    )
    return result


async def run_loop(
    provider,
    system_prompt,
    messages,
    definitions,
    *,
    max_steps,
    model_timeout,
    emit,
    debug,
    cancel,
    transform_context,
    after_tools,
):
    text_only_turns = 0
    specs = [definition.spec for definition in definitions.values()]
    for step in range(1, max_steps + 1):
        emit({"type": "model_started", "step": step})
        started = time.monotonic()
        try:
            turn = await asyncio.wait_for(
                provider.complete(system_prompt, transform_context(messages), specs),
                timeout=model_timeout,
            )
        except TimeoutError:
            return TaskResult(
                "failed", error=f"LLM request timed out after {model_timeout:g} seconds"
            )
        except Exception as exc:
            causes, cause = [], exc
            for _ in range(6):
                if cause is None:
                    break
                causes.append(
                    {
                        "type": type(cause).__name__,
                        "errno": getattr(cause, "errno", None),
                    }
                )
                cause = cause.__cause__ or cause.__context__
            debug(
                {
                    "phase": "model_error",
                    "step": step,
                    "error": redact_secrets(str(exc))[:400],
                    "causes": causes,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            raise
        emit(
            {
                "type": "model_finished",
                "step": step,
                "tool_call_count": len(turn.tool_calls),
            }
        )
        debug(
            {
                "phase": "model_turn",
                "step": step,
                "assistant_text": truncate(turn.text),
                "response_metadata": turn.response_metadata,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": truncate(c.arguments)}
                    for c in turn.tool_calls
                ],
            }
        )
        messages.append(
            ConversationMessage(
                role="assistant",
                text=turn.text,
                tool_calls=turn.tool_calls,
                provider_items=turn.provider_items,
            )
        )
        if turn.text:
            emit(
                {
                    "type": "assistant_text",
                    "text": turn.text[:2000]
                    + ("\n[内容过长，已截断]" if len(turn.text) > 2000 else ""),
                }
            )
        if not turn.tool_calls:
            text_only_turns += 1
            length = turn.response_metadata.get("finish_reason") == "length"
            if text_only_turns >= 2:
                return TaskResult(
                    "failed",
                    error=(
                        "模型在生成工具调用前耗尽了输出预算，请降低推理深度后重试。"
                        if length
                        else "模型连续两轮未返回工具调用，请确认模型支持工具调用。"
                    ),
                )
            messages.append(
                ConversationMessage(
                    role="user",
                    text=(
                        "上一轮达到输出 token 上限，请缩短推理。"
                        if length
                        else "上一轮没有调用工具。"
                    )
                    + "请调用当前可用工具继续；检查并接受产物后调用 finish_task 交付，不要询问用户是否满意。",
                )
            )
            continue
        text_only_turns = 0
        # 放弃优先于同批昂贵动作；finish 必须独占一轮，混合调用仍补齐全部结果。
        abort = next((c for c in turn.tool_calls if c.name == "task_failed"), None)
        calls = [abort] if abort else turn.tool_calls
        results = []
        for call in calls:
            if call.name == "finish_task" and len(calls) != 1:
                result = error_result(
                    call,
                    "TOOL_BATCH_INVALID",
                    "finish_task must be the only tool call in its model turn",
                )
            else:
                result = await execute_tool(
                    call, definitions, emit=emit, debug=debug, cancel=cancel
                )
            if isinstance(result, TaskResult):
                return result
            results.append(result)
        if abort and len(turn.tool_calls) > 1:
            by_id = {r.tool_call_id: r for r in results}
            results = [
                by_id.get(c.id)
                or error_result(c, "TOOL_SKIPPED", "终止请求无效，同批工具未执行。")
                for c in turn.tool_calls
            ]
        messages.append(ConversationMessage(role="tool", tool_results=results))
        stopped = after_tools(results)
        if stopped is not None:
            return stopped
    return TaskResult("failed", error=f"Task exceeded the {max_steps}-step limit")
