"""连接异常保留底层错误类别，不泄露请求凭据、不自动重发请求。"""
import json

import httpx
import pytest
from openai import APIConnectionError

from skill_toolbox.runtime import AgentRuntime, TaskRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("finish, expected", [("length", "耗尽了输出预算"), ("stop", "连续两轮未返回工具调用")])
async def test_missing_calls_distinguish_truncation_from_plain_text(tmp_path, finish, expected):
    from skill_toolbox.models import AssistantTurn

    class Provider:
        calls = 0

        async def complete(self, system, messages, tools):
            self.calls += 1
            if self.calls == 2 and finish == "length":
                assert "缩短推理" in messages[-1].text
            return AssistantTurn(text="(no output)", response_metadata={"finish_reason": finish})

    provider = Provider()
    runtime = AgentRuntime(provider=provider, emit=lambda _: None)
    result = await runtime.run(TaskRequest(skill_id="resume_pro", template_id="t001",
                                          user_prompt="生成测试简历", output_dir=tmp_path,
                                          capabilities={"tool_calling": True, "vision": True},
                                          tool_mode="domain"))
    assert expected in result.error
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_connection_error_is_logged_before_sidecar_handles_failure(tmp_path):
    class Provider:
        model = "test-model"
        max_tokens = 1024
        calls = 0

        async def complete(self, *args):
            self.calls += 1
            request = httpx.Request("POST", "https://example.invalid/chat",
                                    headers={"Authorization": "Bearer private-test-value"})
            transport = httpx.ConnectError("connection reset", request=request)
            transport.__cause__ = ConnectionResetError(10054, "connection reset")
            raise APIConnectionError(request=request) from transport

    provider = Provider()
    logs = []
    runtime = AgentRuntime(provider=provider, emit=lambda _: None, debug_logger=logs.append)
    with pytest.raises(APIConnectionError):
        await runtime.run(TaskRequest(skill_id="resume_pro", template_id="t001",
                                      user_prompt="生成测试简历", output_dir=tmp_path,
                                      capabilities={"tool_calling": True, "vision": True},
                                      tool_mode="domain"))
    error = next(row for row in logs if row["phase"] == "model_error")
    assert [cause["type"] for cause in error["causes"]] == ["APIConnectionError", "ConnectError", "ConnectionResetError"]
    assert error["causes"][-1]["errno"] == 10054
    assert error["elapsed_seconds"] >= 0 and provider.calls == 1
    assert logs[0]["model"] == "test-model" and logs[0]["max_tokens"] == 1024
    assert "private-test-value" not in json.dumps(logs)
