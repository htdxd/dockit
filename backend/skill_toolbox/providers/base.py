from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from skill_toolbox.models import AssistantTurn, ConversationMessage


class ModelProvider(Protocol):
    async def complete(
        self,
        system_prompt: str,
        messages: Sequence[ConversationMessage],
        tools: list[dict[str, object]],
    ) -> AssistantTurn: ...
