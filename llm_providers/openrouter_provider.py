"""OpenRouter backend for the llm_providers abstraction.

OpenRouter exposes an OpenAI-compatible chat-completions API in front of many
third-party models (Llama, DeepSeek, Qwen, ...), including a number of
``:free``-suffixed models with no cost — see ``models.py`` for the curated
picks and https://openrouter.ai/models?max_price=0 for the live list. That
list rotates as providers add/remove free-tier access, so treat the curated
ids as a starting point to verify against your own account, not a guarantee.
"""
from .errors import LLMConfigError
from .openai_compatible import OpenAICompatibleProvider

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"

    def __init__(self, api_key: str, timeout: float = 120.0):
        if not api_key:
            raise LLMConfigError("OPENROUTER_API_KEY is not set")
        super().__init__(api_key=api_key, base_url=BASE_URL, timeout=timeout)
