"""Shared helpers used by more than one module in generators/ (and by
services/scheduler_service.py, which runs these same generators as
scheduled jobs): the recursive NaN/Inf-safe JSON cleaner and the LLM call
helper. Consolidated in Phase 3 Slice 5 — previously each of
dashboard_generator.py, artifact_builder.py, reportgenerator.py, and
scheduler_service.py defined its own near-identical NaN cleaner, and each
of dashboard_generator.py, infographicgenerator.py, ppt_generator.py, and
reportgenerator.py constructed its own OpenAI client with the same
`timeout=120.0` call.

``provider_chat()`` (added alongside the Amazon Bedrock provider) is now the
preferred way for generators to call an LLM — it goes through
``llm_providers`` so the provider (OpenAI/Bedrock) is a config choice
(``DEFAULT_LLM_PROVIDER`` / a per-call override), not a hardcoded SDK call.
``make_openai_client()` is kept for any caller that still needs the raw
OpenAI SDK client directly (e.g. an API only OpenAI exposes).
"""
import os
import math
from typing import Optional

from openai import OpenAI


def clean_nan(obj):
    """Recursively replace NaN/Inf floats with None so the result is always JSON-safe."""
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def make_openai_client() -> OpenAI:
    """Construct the shared-pattern OpenAI client (120s timeout).

    Raises:
        ValueError: If OPENAI_API_KEY is not set. Callers that want to
            degrade gracefully instead of raising should wrap this call in
            their own try/except (see reportgenerator.py).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in environment variables.")
    return OpenAI(api_key=api_key, timeout=120.0)


def provider_chat(*, system: str, user_prompt: str, model: str, provider: Optional[str] = None,
                   temperature: Optional[float] = None, max_tokens: int = 4000,
                   response_format: Optional[dict] = None):
    """Single-turn (system + one user message) chat call via llm_providers.

    Args:
        system: System prompt.
        user_prompt: The single user message.
        model: Model id for whichever provider is selected.
        provider: "openai" / "bedrock" / None (falls back to
            ``DEFAULT_LLM_PROVIDER``, see ``llm_providers.factory.get_provider``).
        temperature: Sampling temperature, or None for the provider default.
        max_tokens: Max tokens to generate.
        response_format: OpenAI's ``{"type": "json_object"}`` switch — honored by
            OpenAI, ignored by providers without a native equivalent (Bedrock);
            keep asking for JSON in the prompt itself so both still produce it.

    Returns:
        A ``llm_providers.types.ChatResult`` (``.content``, ``.usage.prompt_tokens``,
        ``.usage.completion_tokens``).

    Raises:
        llm_providers.errors.LLMError: The provider is misconfigured or the request failed.
    """
    from llm_providers.factory import get_provider
    return get_provider(provider).chat(
        system=system, messages=[{"role": "user", "content": user_prompt}],
        model=model, temperature=temperature, max_tokens=max_tokens,
        response_format=response_format)
