"""Exceptions raised by llm_providers backends."""


class LLMError(Exception):
    """Base class for all errors raised by llm_providers."""


class LLMConfigError(LLMError):
    """A provider is unknown or missing required configuration (API key, region, ...)."""


class LLMRequestError(LLMError):
    """A request to the provider's API failed (network error, HTTP error, malformed response)."""
