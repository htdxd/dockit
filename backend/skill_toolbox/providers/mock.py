from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from skill_toolbox.models import AssistantTurn, ConversationMessage


class ScriptedProvider:
    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self._turns = deque(turns)

    async def complete(
        self,
        system_prompt: str,
        messages: Sequence[ConversationMessage],
        tools: list[dict[str, object]],
    ) -> AssistantTurn:
        del system_prompt, messages, tools
        if not self._turns:
            return AssistantTurn(text="Scripted provider exhausted")
        return self._turns.popleft()

    async def probe(
        self,
        capabilities: list[str] | None = None,
    ) -> dict[str, tuple[str, str | None]]:
        """Offline probe used by tests: every capability is verified."""
        del capabilities
        return {
            "tool_calling": ("verified", None),
            "vision": ("verified", None),
            "reasoning_control": ("verified", "effort"),
        }
