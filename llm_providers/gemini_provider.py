"""Gemini backend for the llm_providers abstraction.

Uses Google's OpenAI-compatibility endpoint
(https://ai.google.dev/gemini-api/docs/openai) rather than the native
google-genai SDK, so it reuses the same chat-completions request/response
handling as every other provider here (``openai_compatible.py``) instead of
a bespoke translator. Free-tier model ids (e.g. ``gemini-2.5-flash``,
``gemini-2.5-flash-lite``) work the same way as paid ones — see
``models.py`` for the curated picks and https://ai.google.dev/gemini-api/docs/pricing
for the current free-tier limits, since Google's free-tier lineup shifts
over time.
"""
from .errors import LLMConfigError
from .openai_compatible import OpenAICompatibleProvider

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiProvider(OpenAICompatibleProvider):
    name = "gemini"

    def __init__(self, api_key: str, timeout: float = 120.0):
        if not api_key:
            raise LLMConfigError("GEMINI_API_KEY is not set")
        super().__init__(api_key=api_key, base_url=BASE_URL, timeout=timeout)
