"""Shared implementation for any LLMProvider backend that speaks the OpenAI
chat-completions wire protocol — OpenAI itself, OpenRouter, and Gemini's
OpenAI-compatibility endpoint all qualify, so the request-building,
response-normalization, and streaming logic lives here once instead of being
copy-pasted per provider. Subclasses just supply ``name`` and a ``base_url``
(``None`` for OpenAI itself).
"""
import logging
from typing import Generator, Optional

from openai import OpenAI

from .base import LLMProvider
from .errors import LLMRequestError
from .types import ChatResult, StreamDelta, Usage

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: Optional[str] = None, timeout: float = 120.0):
        client_kwargs = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)

    def _build_kwargs(self, *, system, messages, model, tools, max_tokens, temperature, stream,
                       response_format=None):
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + list(messages),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format:
            kwargs["response_format"] = response_format
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    def chat(self, *, system, messages, model, tools=None, max_tokens=4000, temperature=None,
              response_format=None) -> ChatResult:
        kwargs = self._build_kwargs(system=system, messages=messages, model=model, tools=tools,
                                     max_tokens=max_tokens, temperature=temperature, stream=False,
                                     response_format=response_format)
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            raise LLMRequestError(f"{self.name} request failed: {e}") from e

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in (msg.tool_calls or [])
        ]
        usage = resp.usage
        return ChatResult(
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0),
            stop_reason=choice.finish_reason,
            raw=resp,
        )

    def stream_chat(self, *, system, messages, model, tools=None, max_tokens=4000,
                     temperature=None) -> Generator[StreamDelta, None, None]:
        kwargs = self._build_kwargs(system=system, messages=messages, model=model, tools=tools,
                                     max_tokens=max_tokens, temperature=temperature, stream=True)

        content_parts = []
        tool_call_accum = {}  # index -> {"id", "name", "arguments"}
        usage = Usage()
        stop_reason = None
        try:
            stream = self._client.chat.completions.create(**kwargs)
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = Usage(prompt_tokens=chunk.usage.prompt_tokens or 0,
                                   completion_tokens=chunk.usage.completion_tokens or 0)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    stop_reason = choice.finish_reason
                delta = choice.delta
                if delta.content:
                    content_parts.append(delta.content)
                    yield StreamDelta(text=delta.content)
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        entry = tool_call_accum.setdefault(tc_delta.index,
                                                            {"id": "", "name": "", "arguments": ""})
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments
        except Exception as e:
            raise LLMRequestError(f"{self.name} streaming request failed: {e}") from e

        tool_calls = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for v in tool_call_accum.values()
        ]
        result = ChatResult(content="".join(content_parts), tool_calls=tool_calls,
                             usage=usage, stop_reason=stop_reason)
        yield StreamDelta(done=True, result=result)
