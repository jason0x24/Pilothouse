"""LLM provider factory.

Pilothouse has exactly two real providers:

  - `MockProvider`  — deterministic replay used when no API key is set
    (powers keyless local demos + the entire test suite).
  - `LiteLLMProvider` — covers every cloud/self-hosted model
    ([LiteLLM](https://docs.litellm.ai/) speaks 100+ APIs). Routing
    happens via the model-id prefix the operator puts in
    `PILOTHOUSE_MODEL_PLANNER`.

`get_provider()` picks one based on:

  1. Explicit `PILOTHOUSE_MODEL_PROVIDER=mock` → mock regardless of keys.
  2. Any LLM API key configured (`PILOTHOUSE_ANTHROPIC_API_KEY`,
     `PILOTHOUSE_OPENROUTER_API_KEY`, `PILOTHOUSE_OPENAI_API_KEY`) →
     `LiteLLMProvider`. LiteLLM also picks up provider-specific env
     vars set in the shell (AWS_*, VERTEX_*, AZURE_*, GROQ_API_KEY, …).
  3. Nothing set → `MockProvider`.

`is_mock_mode()` lets the orchestrator detect whether the runtime will
replay a `mock_plan` so it can inject one into the user message.
"""

from __future__ import annotations

import os

from ...config import Settings, get_settings
from .base import LLMProvider


# Provider-specific environment variables LiteLLM auto-detects. If any of
# these is set in the shell, treat it as "user has a real LLM configured"
# so we don't fall back to mock mode silently.
_THIRD_PARTY_KEY_ENV = (
    "AWS_ACCESS_KEY_ID",  # Bedrock
    "GOOGLE_APPLICATION_CREDENTIALS",  # Vertex
    "GEMINI_API_KEY",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "TOGETHER_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "REPLICATE_API_KEY",
    "PERPLEXITYAI_API_KEY",
    "FIREWORKS_AI_API_KEY",
    "XAI_API_KEY",
)


def get_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    explicit = (s.model_provider or "").strip().lower()

    if explicit == "mock":
        from .mock_provider import MockProvider

        return MockProvider()

    if explicit and explicit != "litellm":
        raise RuntimeError(
            f"Unknown PILOTHOUSE_MODEL_PROVIDER value: {explicit!r}. "
            "Valid values: 'mock', 'litellm', or empty (auto-detect)."
        )

    if explicit == "litellm" or _any_real_key_present(s):
        from .litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            anthropic_api_key=s.anthropic_api_key,
            openrouter_api_key=s.openrouter_api_key,
            openai_api_key=s.openai_api_key,
            openai_base_url=s.openai_base_url,
            openrouter_app_name=s.openrouter_app_name or "Pilothouse",
            openrouter_site_url=s.openrouter_site_url,
        )

    from .mock_provider import MockProvider

    return MockProvider()


def is_mock_mode(settings: Settings | None = None) -> bool:
    """True when no real LLM provider is configured.

    The orchestrator uses this to decide whether to inject a `mock_plan`
    into the user message so the runtime can replay it deterministically.
    Cheap to call — does not construct the provider.
    """
    s = settings or get_settings()
    explicit = (s.model_provider or "").strip().lower()
    if explicit == "mock":
        return True
    if explicit == "litellm":
        return False
    return not _any_real_key_present(s)


def _any_real_key_present(s: Settings) -> bool:
    if s.anthropic_api_key or s.openrouter_api_key or s.openai_api_key:
        return True
    return any(os.environ.get(k) for k in _THIRD_PARTY_KEY_ENV)


__all__ = ["get_provider", "is_mock_mode", "LLMProvider"]
