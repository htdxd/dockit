"""桌面与网页共用的任务输入、结果和问答通道。"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class TaskRequest:
    skill_id: str
    user_prompt: str
    output_dir: Path
    materials: list[Path] = field(default_factory=list)
    template_id: str | None = field(default=None, kw_only=True)
    output_format: str | None = field(default=None, kw_only=True)
    writing_style: Literal["light", "balanced", "strong"] = field(
        default="balanced", kw_only=True
    )
    capabilities: dict[str, bool] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    mineru_token: str | None = None
    # 接受现有调用方的 domain 参数；legacy 明确退役，不再恢复旧执行路径。
    tool_mode: str | None = None


@dataclass(frozen=True)
class TaskResult:
    status: str
    artifacts: list[Path] = field(default_factory=list)
    error: str | None = None


class UserInputBroker:
    def __init__(self):
        self._future = None

    async def wait(self):
        if self._future is not None and not self._future.done():
            raise RuntimeError("A question request is already pending")
        self._future = asyncio.get_running_loop().create_future()
        return await self._future

    def answer(self, answers):
        if self._future is None or self._future.done():
            raise RuntimeError("No question request is pending")
        self._future.set_result(answers)
