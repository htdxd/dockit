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

    # Optional, but every real provider implements it: run a small deterministic
    # probe to verify tool calling / vision / reasoning control. Return a dict
    # keyed by capability with a (status, detail) tuple; the sidecar folds it
    # into a versioned CapabilityProbeReport. Implementations must not write raw
    # CoT or API keys into logs/UI — details stay short and sanitised.
    async def probe(
        self,
        capabilities: list[str] | None = None,
    ) -> dict[str, tuple[str, str | None]]: ...
