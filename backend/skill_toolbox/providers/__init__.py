from skill_toolbox.models import ProviderConfig
from skill_toolbox.providers.anthropic import AnthropicProvider
from skill_toolbox.providers.base import ModelProvider
from skill_toolbox.providers.openai import OpenAIProvider


def create_provider(config: ProviderConfig) -> ModelProvider:
    if config.kind in {"openai", "openai_compatible"}:
        return OpenAIProvider(config)
    if config.kind == "anthropic":
        return AnthropicProvider(config)
    raise ValueError(f"Provider kind is not available: {config.kind}")


__all__ = ["ModelProvider", "create_provider"]
