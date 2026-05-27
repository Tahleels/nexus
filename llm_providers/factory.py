"""Resolves a configured LLMProvider instance by name.

Providers are cached per-process (one client instance reused across calls) —
the same pattern already used by ``generators/generator_utils.make_openai_client()``
and ``services/workspace_openai_service.py``'s module-level client, just
extended to more than one provider.

Provider/model selection is data, not code: which provider a given hub
agent, workspace conversation, or generator call uses comes from a DB column
or an env var (see each call site), never a hardcoded branch here. Switching
providers is therefore a config change, not a deploy.
"""
import os
import threading
from typing import Optional

from .base import LLMProvider
from .errors import LLMConfigError
from .models import SUPPORTED_PROVIDERS

_lock = threading.Lock()
_instances: dict = {}


def get_provider(name: Optional[str] = None) -> LLMProvider:
    """Return the (cached) provider instance for ``name``.

    Args:
        name: ``"gemini"``, ``"openrouter"``, or ``"bedrock"``, case-insensitive.
            None (or empty) falls back to the ``DEFAULT_LLM_PROVIDER`` env var,
            itself defaulting to ``"gemini"``. ``"openai"`` is parked (see
            ``_build`` below) and is not in ``SUPPORTED_PROVIDERS``.

    Raises:
        LLMConfigError: Unknown provider name, or the provider is missing
            required configuration (API key / AWS region).
    """
    resolved = (name or os.getenv("DEFAULT_LLM_PROVIDER") or "gemini").strip().lower()
    if resolved not in SUPPORTED_PROVIDERS:
        raise LLMConfigError(
            f"Unknown LLM provider '{resolved}'. Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    with _lock:
        if resolved not in _instances:
            _instances[resolved] = _build(resolved)
        return _instances[resolved]


def _build(name: str) -> LLMProvider:
    # OpenAI is parked for now — kept here (commented) so re-enabling later
    # is a config change, not a rewrite. Re-add "openai" to SUPPORTED_PROVIDERS
    # in models.py to bring it back.
    # if name == "openai":
    #     from .openai_provider import OpenAIProvider
    #     return OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY", ""))
    if name == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(api_key=os.getenv("GEMINI_API_KEY", ""))
    if name == "openrouter":
        from .openrouter_provider import OpenRouterProvider
        return OpenRouterProvider(api_key=os.getenv("OPENROUTER_API_KEY", ""))
    if name == "bedrock":
        from .bedrock_provider import BedrockProvider
        return BedrockProvider(
            region=os.getenv("AWS_REGION", ""),
            access_key=os.getenv("AWS_ACCESS_KEY_ID") or None,
            secret_key=os.getenv("AWS_SECRET_ACCESS_KEY") or None,
            session_token=os.getenv("AWS_SESSION_TOKEN") or None,
        )
    raise LLMConfigError(f"Unknown LLM provider '{name}'")
