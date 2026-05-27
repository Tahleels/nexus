"""Amazon Bedrock backend for the llm_providers abstraction.

Talks to Bedrock's `Converse` / `ConverseStream` runtime API — a single
request/response shape that works across every model family Bedrock hosts
(Anthropic, Amazon Nova, Meta Llama, Mistral, ...), so this one adapter
covers all of them; no per-model-family branching needed.

Callers still build requests in OpenAI's chat-completions message/tool shape
(see ``types.py`` module docstring) — this module's job is translating that
shape to/from Bedrock's Converse shape, so nothing upstream of the provider
needs to know Bedrock exists.

Auth: this class only ever sets explicit IAM keys (or falls back to boto3's
own default credential chain). Some accounts also need/prefer a Bedrock
"API key" (bearer token) instead — botocore resolves that automatically from
an ``AWS_BEARER_TOKEN_BEDROCK`` env var if present (derived from the
service's signing name; requires botocore new enough to declare
``smithy.api#httpBearerAuth`` for bedrock-runtime, see requirements.txt),
taking priority over the IAM keys below with zero code changes needed here.
See LOCAL_TESTING_GUIDE.md §6.1 for when you need this.
"""
import json
import logging
from typing import Generator, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .base import LLMProvider
from .errors import LLMConfigError, LLMRequestError
from .types import ChatResult, StreamDelta, Usage

logger = logging.getLogger(__name__)


def _parse_tool_input(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _to_bedrock_message(msg: dict) -> Optional[dict]:
    """Convert one OpenAI-shaped chat message into a Bedrock Converse message.

    Returns None for system messages (handled separately via the top-level
    ``system`` field, same as every other provider here).
    """
    role = msg.get("role")

    if role == "tool":
        # Tool-result message -> Bedrock represents this as a "user" turn
        # carrying a toolResult content block (Anthropic-native-API style,
        # which Bedrock's Converse API adopted for every model family).
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        return {
            "role": "user",
            "content": [{"toolResult": {
                "toolUseId": msg.get("tool_call_id", ""),
                "content": [{"text": text}],
            }}],
        }

    if role == "assistant":
        blocks = []
        text = msg.get("content")
        if text:
            blocks.append({"text": text})
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            blocks.append({"toolUse": {
                "toolUseId": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": _parse_tool_input(fn.get("arguments")),
            }})
        if not blocks:
            blocks.append({"text": ""})
        return {"role": "assistant", "content": blocks}

    if role == "user":
        return {"role": "user", "content": [{"text": msg.get("content", "")}]}

    # Unknown role — skip rather than send something Bedrock will reject outright.
    logger.warning("BedrockProvider: skipping message with unsupported role %r", role)
    return None


def _to_bedrock_messages(messages: list) -> list:
    """Map + merge adjacent same-role messages.

    Bedrock's Converse API enforces strict user/assistant alternation, but
    callers here (e.g. the Hub orchestrator) append one ``role: "tool"``
    message per tool call in the same turn — several consecutive entries
    that all become Bedrock "user" messages. Those must be combined into a
    single message with multiple toolResult blocks, not sent as separate
    consecutive "user" turns.
    """
    converted = []
    for msg in messages:
        bedrock_msg = _to_bedrock_message(msg)
        if bedrock_msg is None:
            continue
        if converted and converted[-1]["role"] == bedrock_msg["role"]:
            converted[-1]["content"].extend(bedrock_msg["content"])
        else:
            converted.append(bedrock_msg)
    return converted


def _to_bedrock_tool_config(tools: Optional[list]) -> Optional[dict]:
    if not tools:
        return None
    tool_specs = []
    for t in tools:
        fn = t.get("function", t)
        tool_specs.append({"toolSpec": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "inputSchema": {"json": fn.get("parameters", {"type": "object", "properties": {}})},
        }})
    return {"tools": tool_specs, "toolChoice": {"auto": {}}}


def _from_bedrock_content(blocks: list) -> tuple:
    """Split Bedrock content blocks into (text, OpenAI-shaped tool_calls)."""
    text_parts = []
    tool_calls = []
    for block in blocks or []:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append({
                "id": tu.get("toolUseId", ""),
                "type": "function",
                "function": {"name": tu.get("name", ""), "arguments": json.dumps(tu.get("input", {}))},
            })
    return "".join(text_parts), tool_calls


class BedrockProvider(LLMProvider):
    name = "bedrock"

    def __init__(self, region: str, access_key: Optional[str] = None,
                 secret_key: Optional[str] = None, session_token: Optional[str] = None):
        if not region:
            raise LLMConfigError("AWS_REGION is not set (required for Amazon Bedrock)")
        # access_key/secret_key/session_token are optional: when unset, boto3
        # falls back to its own default credential chain (IAM role, instance
        # profile, ~/.aws/credentials, etc) rather than failing outright.
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            aws_session_token=session_token or None,
        )

    def _build_request(self, *, system, messages, model, tools, max_tokens, temperature) -> dict:
        req = {
            "modelId": model,
            "messages": _to_bedrock_messages(messages),
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            req["system"] = [{"text": system}]
        if temperature is not None:
            req["inferenceConfig"]["temperature"] = temperature
        tool_config = _to_bedrock_tool_config(tools)
        if tool_config:
            req["toolConfig"] = tool_config
        return req

    def chat(self, *, system, messages, model, tools=None, max_tokens=4000, temperature=None,
              response_format=None) -> ChatResult:
        # response_format ("json_object" mode) has no Bedrock Converse equivalent —
        # every current caller already asks for JSON in the prompt itself as a
        # fallback, so this is a silent no-op rather than an error.
        req = self._build_request(system=system, messages=messages, model=model, tools=tools,
                                   max_tokens=max_tokens, temperature=temperature)
        try:
            resp = self._client.converse(**req)
        except (ClientError, BotoCoreError) as e:
            raise LLMRequestError(f"Bedrock Converse request failed: {e}") from e

        content, tool_calls = _from_bedrock_content(resp["output"]["message"]["content"])
        usage = resp.get("usage", {})
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=usage.get("inputTokens", 0),
                        completion_tokens=usage.get("outputTokens", 0)),
            stop_reason=resp.get("stopReason"),
            raw=resp,
        )

    def stream_chat(self, *, system, messages, model, tools=None, max_tokens=4000,
                     temperature=None) -> Generator[StreamDelta, None, None]:
        req = self._build_request(system=system, messages=messages, model=model, tools=tools,
                                   max_tokens=max_tokens, temperature=temperature)
        try:
            resp = self._client.converse_stream(**req)
        except (ClientError, BotoCoreError) as e:
            raise LLMRequestError(f"Bedrock ConverseStream request failed: {e}") from e

        content_parts = []
        tool_call_accum = {}  # blockIndex -> {"id", "name", "arguments"}
        usage = Usage()
        stop_reason = None
        try:
            for event in resp["stream"]:
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    tool_use = start.get("start", {}).get("toolUse")
                    if tool_use:
                        tool_call_accum[start["contentBlockIndex"]] = {
                            "id": tool_use.get("toolUseId", ""),
                            "name": tool_use.get("name", ""),
                            "arguments": "",
                        }
                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"]
                    idx = event["contentBlockDelta"]["contentBlockIndex"]
                    if "text" in delta:
                        content_parts.append(delta["text"])
                        yield StreamDelta(text=delta["text"])
                    elif "toolUse" in delta:
                        entry = tool_call_accum.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        entry["arguments"] += delta["toolUse"].get("input", "")
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    u = event["metadata"].get("usage", {})
                    if u:
                        usage = Usage(prompt_tokens=u.get("inputTokens", 0),
                                       completion_tokens=u.get("outputTokens", 0))
        except (ClientError, BotoCoreError) as e:
            raise LLMRequestError(f"Bedrock ConverseStream streaming failed: {e}") from e

        tool_calls = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for v in tool_call_accum.values()
        ]
        result = ChatResult(content="".join(content_parts), tool_calls=tool_calls,
                             usage=usage, stop_reason=stop_reason)
        yield StreamDelta(done=True, result=result)
