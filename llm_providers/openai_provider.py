"""OpenAI backend for the llm_providers abstraction.

Parked for now — not registered in ``factory.py``'s supported-provider set
(see ``models.py``), so nothing selects it by default. Kept on disk, not
deleted, so re-enabling it later is a config change, not a rewrite.
"""
from .errors import LLMConfigError
from .openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, api_key: str, timeout: float = 120.0):
        if not api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set")
        super().__init__(api_key=api_key, timeout=timeout)
