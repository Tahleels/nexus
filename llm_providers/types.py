"""Provider-agnostic request/response shapes for llm_providers.

The wire format for ``messages`` and ``tools`` mirrors OpenAI's chat-completions
API deliberately, not as an implementation detail leaking through, but because
that shape was already the de facto internal contract every call site in this
codebase builds (see ``agents/core/orchestrator/engine.py``'s
``ContextBuilder.build_tool_definitions`` and its tool-call/tool-result message
construction, and ``services/workspace_openai_service.py``). Reusing it here
means adding a provider only requires a translator in that provider's own
module (see ``bedrock_provider.py``) rather than rewriting every caller.
"""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Usage:
    """Token accounting for one chat call, in OpenAI's own field names."""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


@dataclass
class ChatResult:
    """Normalized result of a single (non-streaming) chat call.

    ``tool_calls`` uses OpenAI's own wire shape —
    ``[{"id": ..., "type": "function", "function": {"name": ..., "arguments": "<json str>"}}]``
    — so existing call sites that parse ``tc["function"]["name"]`` /
    ``json.loads(tc["function"]["arguments"])`` need no changes beyond reading
    from ``result.tool_calls`` instead of a raw HTTP response body.
    """
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: Optional[str] = None
    raw: Any = None


@dataclass
class StreamDelta:
    """One increment of a streamed chat call.

    Either a text fragment (``text``) or, on the final delta, ``done=True``
    with the fully-accumulated ``result`` attached (content, tool calls, and
    usage collected over the whole stream) — mirroring how
    ``services/workspace_openai_service.py`` already consumes OpenAI's SDK
    stream today, just provider-agnostic.
    """
    text: str = ""
    done: bool = False
    result: Optional[ChatResult] = None
