"""LLM provider registry + factory.

Pilothouse currently ships three real providers — `anthropic`,
`openai`, `openrouter` — plus the deterministic `mock` provider used
in tests and keyless local demos.

Adding a new provider
---------------------

1. Write a class in `pilothouse/agent/providers/<name>_provider.py`
   that fits the `LLMProvider` protocol (`base.py`).
2. Add one row to `PROVIDER_FACTORIES` below, mapping the provider
   name to a callable `(Settings) -> LLMProvider`.
3. Add the credential field(s) it needs to `Settings` in
   `pilothouse/config.py`.

That's all — `get_provider()` and the auto-detect logic pick it up
for free. The runtime, orchestration layer, CLI, templates, and tests
never need to change.

Selection
---------

`get_provider(settings)` resolves a provider by:

  1. Explicit `settings.model_provider`, when set, must match a
     registered name. Errors out clearly if the matching key is missing.
  2. Otherwise auto-detect: first non-empty API key wins, in the
     priority order declared in `_AUTO_DETECT_ORDER`.
  3. No keys + no explicit → `MockProvider`.

`is_mock_mode()` is a cheap predicate the orchestrator uses to decide
whether to wrap the agent's user message with a deterministic
`mock_plan` so the runtime can replay it without a real LLM call.
"""

from __future__ import annotations

from typing import Callable

from ...config import Settings, get_settings
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .mock_provider import MockProvider
from .openai_compat import OpenAIProvider, OpenRouterProvider


_ProviderFactory = Callable[[Settings], LLMProvider]


PROVIDER_FACTORIES: dict[str, _ProviderFactory] = {
    "anthropic": lambda s: AnthropicProvider(api_key=s.anthropic_api_key),
    "openai": lambda s: OpenAIProvider(
        api_key=s.openai_api_key,
        base_url=s.openai_base_url,
    ),
    "openrouter": lambda s: OpenRouterProvider(
        api_key=s.openrouter_api_key,
        app_name=s.openrouter_app_name or "Pilothouse",
        site_url=s.openrouter_site_url,
    ),
    "mock": lambda s: MockProvider(),
}


# Which provider auto-wins when multiple keys are set. Anthropic first
# preserves the historical default for existing installs.
_AUTO_DETECT_ORDER: tuple[str, ...] = ("anthropic", "openrouter", "openai")


# How a provider's settings field is named; used by auto-detect to
# decide which one has a real key.
_PROVIDER_KEY_FIELDS: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "openrouter": "openrouter_api_key",
    "openai": "openai_api_key",
}


def get_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    name = _resolve_provider_name(s)
    factory = PROVIDER_FACTORIES.get(name)
    if factory is None:
        raise RuntimeError(
            f"Unknown PILOTHOUSE_MODEL_PROVIDER value: {name!r}. "
            f"Valid values: {', '.join(sorted(PROVIDER_FACTORIES))}."
        )
    if name != "mock":
        _require_credential(name, s)
    return factory(s)


def is_mock_mode(settings: Settings | None = None) -> bool:
    """True when the runtime will replay a `mock_plan` instead of
    calling a real LLM. Cheap — does not construct any provider."""
    s = settings or get_settings()
    explicit = (s.model_provider or "").strip().lower()
    if explicit == "mock":
        return True
    if explicit in PROVIDER_FACTORIES and explicit != "mock":
        return False
    # Auto-detect path: mock only when no real key is present.
    return not _any_key_present(s)


def _resolve_provider_name(s: Settings) -> str:
    explicit = (s.model_provider or "").strip().lower()
    if explicit:
        return explicit
    for name in _AUTO_DETECT_ORDER:
        key_field = _PROVIDER_KEY_FIELDS.get(name)
        if key_field and getattr(s, key_field, ""):
            return name
    return "mock"


def _require_credential(provider: str, s: Settings) -> None:
    key_field = _PROVIDER_KEY_FIELDS.get(provider)
    if not key_field:
        return  # provider has no key field — nothing to check
    if not getattr(s, key_field, ""):
        env_name = f"PILOTHOUSE_{key_field.upper()}"
        raise RuntimeError(
            f"PILOTHOUSE_MODEL_PROVIDER={provider!r} but {env_name} is empty."
        )


def _any_key_present(s: Settings) -> bool:
    return any(
        getattr(s, field, "") for field in _PROVIDER_KEY_FIELDS.values()
    )


def supported_providers() -> tuple[str, ...]:
    """Names of every registered provider, mock included. Stable order:
    real providers in auto-detect order, then mock."""
    return _AUTO_DETECT_ORDER + ("mock",)


__all__ = [
    "PROVIDER_FACTORIES",
    "get_provider",
    "is_mock_mode",
    "supported_providers",
    "LLMProvider",
]
