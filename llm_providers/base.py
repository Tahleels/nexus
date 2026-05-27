"""Provider-agnostic chat interface implemented by every llm_providers backend."""
from abc import ABC, abstractmethod
from typing import Generator, Optional

from .types import ChatResult, StreamDelta


class LLMProvider(ABC):
    """One instance per provider (OpenAI, Bedrock, ...); ``model`` is passed
    per-call so a single provider instance serves every model it hosts —
    matching how callers already select a model per hub agent / conversation.
    """

    #: Short, lowercase provider identifier (e.g. ``"openai"``, ``"bedrock"``).
    #: Matches the ``provider`` values stored in the DB / read from config.
    name: str

    @abstractmethod
    def chat(self, *, system: str, messages: list, model: str,
              tools: Optional[list] = None, max_tokens: int = 4000,
              temperature: Optional[float] = None,
              response_format: Optional[dict] = None) -> ChatResult:
        """Make one non-streaming chat call.

        Args:
            system: System prompt text.
            messages: Chat history in OpenAI chat-completions message shape
                (``{"role": "user"|"assistant"|"tool", "content": ..., ...}``).
            model: Provider-specific model id.
            tools: OpenAI function-calling tool definitions
                (``[{"type": "function", "function": {...}}]``), or None.
            max_tokens: Max tokens to generate.
            temperature: Sampling temperature, or None to use the provider default.
            response_format: OpenAI's ``{"type": "json_object"}`` structured-output
                switch. Only OpenAI natively supports forcing this; providers that
                don't (Bedrock) ignore it and rely on the prompt's own JSON
                instructions instead — safe to always pass when the system/user
                prompt already asks for JSON, which every current caller does.

        Returns:
            A normalized ``ChatResult``.

        Raises:
            LLMRequestError: The provider call failed.
        """
        raise NotImplementedError

    @abstractmethod
    def stream_chat(self, *, system: str, messages: list, model: str,
                     tools: Optional[list] = None, max_tokens: int = 4000,
                     temperature: Optional[float] = None) -> Generator[StreamDelta, None, None]:
        """Stream a chat call. Same arguments as ``chat()``.

        Yields:
            ``StreamDelta`` text fragments, followed by one final delta with
            ``done=True`` and the fully-accumulated ``result``.

        Raises:
            LLMRequestError: The provider call failed.
        """
        raise NotImplementedError
