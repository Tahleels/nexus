"""Curated model catalog surfaced to pickers (Hub agent builder, Workspace
chat). Not exhaustive — both providers host far more models than this: it's
the "known good, worth defaulting to" subset, kept in one place so the UI and
backend defaults never drift apart.

Adding a model users can already reach on their own account/AWS access does
not require a code change elsewhere: any model id can still be typed/stored
directly (see ``hub_agents.model`` / ``ws_conversations.model``), this list
only drives the convenience dropdowns.
"""
import os

# Parked for now — not registered in MODELS_BY_PROVIDER below, so it won't
# surface in pickers. Left here so re-enabling OpenAI is a one-line change.
# OPENAI_MODELS = [
#     {"id": "gpt-4o", "label": "GPT-4o"},
#     {"id": "gpt-4o-mini", "label": "GPT-4o mini"},
#     {"id": "gpt-4.1", "label": "GPT-4.1"},
#     {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini"},
#     {"id": "gpt-4.1-nano", "label": "GPT-4.1 nano"},
#     {"id": "o4-mini", "label": "o4-mini (reasoning)"},
# ]

# Free-tier as of writing (see https://ai.google.dev/gemini-api/docs/pricing) —
# Google's free-tier lineup shifts over time, so re-check that page if calls
# start getting rejected for billing reasons.
GEMINI_MODELS = [
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite"},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
]

# ":free"-suffixed models cost nothing on OpenRouter, but the exact free
# lineup rotates as underlying providers add/remove free-tier access — check
# https://openrouter.ai/models?max_price=0 and swap these ids if one goes away.
OPENROUTER_MODELS = [
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "label": "Llama 3.3 70B (free)"},
    {"id": "deepseek/deepseek-chat-v3.1:free", "label": "DeepSeek Chat v3.1 (free)"},
    {"id": "qwen/qwen3-235b-a22b:free", "label": "Qwen3 235B (free)"},
]

BEDROCK_MODELS = [
    {"id": "anthropic.claude-sonnet-5", "label": "Claude Sonnet 5 (Anthropic on Bedrock)"},
    {"id": "anthropic.claude-opus-5", "label": "Claude Opus 5 (Anthropic on Bedrock)"},
    {"id": "anthropic.claude-haiku-4-5-20251001-v1:0", "label": "Claude Haiku 4.5 (Anthropic on Bedrock)"},
    {"id": "amazon.nova-2-lite-v1:0", "label": "Amazon Nova 2 Lite"},
    {"id": "mistral.mistral-large-3-675b-instruct", "label": "Mistral Large 3"},
    {"id": "qwen.qwen3-235b-a22b-2507-v1:0", "label": "Qwen3 235B"},
]

MODELS_BY_PROVIDER = {
    # "openai": OPENAI_MODELS,  # parked — see factory.py
    "gemini": GEMINI_MODELS,
    "openrouter": OPENROUTER_MODELS,
    "bedrock": BEDROCK_MODELS,
}

SUPPORTED_PROVIDERS = tuple(MODELS_BY_PROVIDER.keys())


def default_model_for(provider: str) -> str:
    """First (recommended) model id for a provider, used when an agent/
    conversation record has a provider but no model set yet."""
    models = MODELS_BY_PROVIDER.get(provider, [])
    return models[0]["id"] if models else ""


# Two cost/quality tiers, one id per provider — lets call sites express
# "I want the fast/cheap model" or "I want the quality model" without
# hardcoding a provider-specific id, so DEFAULT_LLM_PROVIDER can be flipped
# without also hunting down every *_MODEL env var default in the codebase.
FAST_MODEL_BY_PROVIDER = {
    # "openai":  "gpt-4o-mini",  # parked — see factory.py
    "gemini":     "gemini-2.5-flash-lite",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "bedrock":    "anthropic.claude-haiku-4-5-20251001-v1:0",
}
QUALITY_MODEL_BY_PROVIDER = {
    # "openai":  "gpt-4o",  # parked — see factory.py
    "gemini":     "gemini-2.5-flash",
    "openrouter": "deepseek/deepseek-chat-v3.1:free",
    "bedrock":    "anthropic.claude-sonnet-5",
}


def fast_model_for(provider: str) -> str:
    """Cheap/fast model id for a provider (falls back to the gemini tier if unknown)."""
    return FAST_MODEL_BY_PROVIDER.get(provider, FAST_MODEL_BY_PROVIDER["gemini"])


def quality_model_for(provider: str) -> str:
    """Higher-quality model id for a provider (falls back to the gemini tier if unknown)."""
    return QUALITY_MODEL_BY_PROVIDER.get(provider, QUALITY_MODEL_BY_PROVIDER["gemini"])


def resolve_default_model(env_var: str, tier: str = "fast") -> str:
    """Resolve a call site's default model: explicit env override if set,
    otherwise the ``tier`` ("fast"/"quality") model for whichever provider
    ``DEFAULT_LLM_PROVIDER`` currently names.

    This is what lets switching ``DEFAULT_LLM_PROVIDER`` (e.g. to "bedrock")
    work without also setting every individual ``*_MODEL`` env var — each
    call site already declares which cost/quality tier it wants; only the
    provider changes.
    """
    explicit = os.getenv(env_var)
    if explicit:
        return explicit
    provider = os.getenv("DEFAULT_LLM_PROVIDER", "gemini").strip().lower()
    return quality_model_for(provider) if tier == "quality" else fast_model_for(provider)
