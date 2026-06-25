"""Unit tests for llm_providers — the provider-agnostic LLM abstraction.

Pure unit tests: no DB, no Flask app, no real network calls (SDK clients are
monkeypatched). Deliberately does not use the `flask_app`/`*_client` fixtures
in conftest.py, unlike the rest of tests/, since this module has no
dependency on the running app or its database.
"""
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from llm_providers.errors import LLMConfigError, LLMRequestError
from llm_providers.types import ChatResult, StreamDelta, Usage
from llm_providers.bedrock_provider import (
    _to_bedrock_messages, _to_bedrock_tool_config, _from_bedrock_content,
)


# ─── factory ──────────────────────────────────────────────────────────────

def test_get_provider_rejects_unknown_name():
    from llm_providers.factory import get_provider
    with pytest.raises(LLMConfigError):
        get_provider("made-up-provider")


def test_get_provider_openai_is_parked(monkeypatch):
    """OpenAI is intentionally not in SUPPORTED_PROVIDERS — see factory.py."""
    import llm_providers.factory as factory
    factory._instances.clear()
    with pytest.raises(LLMConfigError):
        factory.get_provider("openai")


def test_get_provider_gemini_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import llm_providers.factory as factory
    factory._instances.clear()
    with pytest.raises(LLMConfigError):
        factory.get_provider("gemini")


def test_get_provider_openrouter_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import llm_providers.factory as factory
    factory._instances.clear()
    with pytest.raises(LLMConfigError):
        factory.get_provider("openrouter")


def test_get_provider_bedrock_requires_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    import llm_providers.factory as factory
    factory._instances.clear()
    with pytest.raises(LLMConfigError):
        factory.get_provider("bedrock")


def test_get_provider_caches_instance(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    import llm_providers.factory as factory
    factory._instances.clear()
    a = factory.get_provider("gemini")
    b = factory.get_provider("gemini")
    assert a is b


# ─── Bedrock message translation ───────────────────────────────────────────

def test_to_bedrock_messages_merges_consecutive_tool_results():
    """Bedrock's Converse API requires strict user/assistant alternation, but
    the Hub orchestrator appends one role:"tool" message per tool call in a
    turn — those must collapse into a single "user" message, not N separate
    consecutive "user" turns (which Bedrock rejects)."""
    messages = [
        {"role": "user", "content": "do two things"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "calc", "arguments": '{"expr": "2+2"}'}},
            {"id": "call_2", "type": "function",
             "function": {"name": "weather", "arguments": '{"city": "NYC"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "4"},
        {"role": "tool", "tool_call_id": "call_2", "content": "sunny"},
    ]
    converted = _to_bedrock_messages(messages)

    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert len(converted[1]["content"]) == 2  # two toolUse blocks
    assert len(converted[2]["content"]) == 2  # two toolResult blocks merged into one message
    assert converted[2]["content"][0]["toolResult"]["toolUseId"] == "call_1"
    assert converted[2]["content"][1]["toolResult"]["toolUseId"] == "call_2"


def test_to_bedrock_messages_plain_user_assistant():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    converted = _to_bedrock_messages(messages)
    assert converted == [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "hello"}]},
    ]


def test_to_bedrock_tool_config_converts_openai_shape():
    tools = [{"type": "function", "function": {
        "name": "calc", "description": "calculator",
        "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}},
    }}]
    cfg = _to_bedrock_tool_config(tools)
    assert cfg["tools"][0]["toolSpec"]["name"] == "calc"
    assert cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]["expr"]["type"] == "string"


def test_to_bedrock_tool_config_none_when_no_tools():
    assert _to_bedrock_tool_config(None) is None
    assert _to_bedrock_tool_config([]) is None


def test_from_bedrock_content_splits_text_and_tool_use():
    blocks = [
        {"text": "hello "},
        {"toolUse": {"toolUseId": "t1", "name": "calc", "input": {"expr": "2+2"}}},
        {"text": "world"},
    ]
    content, tool_calls = _from_bedrock_content(blocks)
    assert content == "hello world"
    assert tool_calls == [{
        "id": "t1", "type": "function",
        "function": {"name": "calc", "arguments": '{"expr": "2+2"}'},
    }]


# ─── BedrockProvider.chat() against a stubbed boto3 client ─────────────────

class _StubBedrockClient:
    def __init__(self, response):
        self._response = response
        self.last_request = None

    def converse(self, **kwargs):
        self.last_request = kwargs
        return self._response


def test_bedrock_provider_chat_happy_path(monkeypatch):
    from llm_providers.bedrock_provider import BedrockProvider

    stub_response = {
        "output": {"message": {"content": [{"text": "The answer is 4."}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    stub_client = _StubBedrockClient(stub_response)
    monkeypatch.setattr("boto3.client", lambda *a, **kw: stub_client)

    provider = BedrockProvider(region="us-east-1", access_key="AKIA", secret_key="secret")
    result = provider.chat(system="You are helpful.", messages=[{"role": "user", "content": "2+2?"}],
                            model="anthropic.claude-3-5-sonnet-20241022-v2:0")

    assert isinstance(result, ChatResult)
    assert result.content == "The answer is 4."
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    assert stub_client.last_request["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert stub_client.last_request["system"] == [{"text": "You are helpful."}]


def test_bedrock_provider_chat_wraps_client_error(monkeypatch):
    from botocore.exceptions import ClientError
    from llm_providers.bedrock_provider import BedrockProvider

    class _RaisingClient:
        def converse(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "Converse")

    monkeypatch.setattr("boto3.client", lambda *a, **kw: _RaisingClient())
    provider = BedrockProvider(region="us-east-1")
    with pytest.raises(LLMRequestError):
        provider.chat(system="", messages=[{"role": "user", "content": "hi"}], model="anthropic.claude-3-5-sonnet-20241022-v2:0")


# ─── langchain_factory (nlq_engine.py's Bedrock support) ───────────────────

def test_langchain_factory_openai_is_parked(monkeypatch):
    """OpenAI's branch is commented out in langchain_factory.py — falls
    through to the "unknown provider" error same as any unsupported name."""
    from llm_providers.langchain_factory import get_langchain_chat_model
    with pytest.raises(LLMConfigError):
        get_langchain_chat_model(provider="openai")


def test_langchain_factory_gemini_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from llm_providers.langchain_factory import get_langchain_chat_model
    with pytest.raises(LLMConfigError):
        get_langchain_chat_model(provider="gemini")


def test_langchain_factory_bedrock_requires_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    from llm_providers.langchain_factory import get_langchain_chat_model
    with pytest.raises(LLMConfigError):
        get_langchain_chat_model(provider="bedrock")


def test_langchain_factory_returns_chat_openai_for_gemini(monkeypatch):
    """Gemini and OpenRouter both ride LangChain's ChatOpenAI, pointed at
    their own OpenAI-compatible base_url — see langchain_factory.py."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from llm_providers.langchain_factory import get_langchain_chat_model
    from llm_providers.gemini_provider import BASE_URL as GEMINI_BASE_URL
    from langchain_openai import ChatOpenAI
    model = get_langchain_chat_model(provider="gemini", model="gemini-2.5-flash-lite")
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == GEMINI_BASE_URL


def test_langchain_factory_returns_chat_openai_for_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from llm_providers.langchain_factory import get_langchain_chat_model
    from llm_providers.openrouter_provider import BASE_URL as OPENROUTER_BASE_URL
    from langchain_openai import ChatOpenAI
    model = get_langchain_chat_model(provider="openrouter",
                                      model="meta-llama/llama-3.3-70b-instruct:free")
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == OPENROUTER_BASE_URL


def test_langchain_factory_returns_chat_bedrock_converse(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    from llm_providers.langchain_factory import get_langchain_chat_model
    from langchain_aws import ChatBedrockConverse
    model = get_langchain_chat_model(provider="bedrock")
    assert isinstance(model, ChatBedrockConverse)
    assert model.region_name == "us-east-1"


def test_langchain_factory_unknown_provider(monkeypatch):
    from llm_providers.langchain_factory import get_langchain_chat_model
    with pytest.raises(LLMConfigError):
        get_langchain_chat_model(provider="made-up")


# ─── resolve_default_model — the "switch DEFAULT_LLM_PROVIDER, everything
# else adapts" guarantee ────────────────────────────────────────────────────

def test_resolve_default_model_uses_explicit_env_override(monkeypatch):
    monkeypatch.setenv("SOME_MODEL", "custom-model-id")
    from llm_providers.models import resolve_default_model
    assert resolve_default_model("SOME_MODEL", tier="fast") == "custom-model-id"


def test_resolve_default_model_follows_provider_default_gemini(monkeypatch):
    monkeypatch.delenv("SOME_MODEL", raising=False)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "gemini")
    from llm_providers.models import resolve_default_model
    assert resolve_default_model("SOME_MODEL", tier="fast") == "gemini-2.5-flash-lite"
    assert resolve_default_model("SOME_MODEL", tier="quality") == "gemini-2.5-flash"


def test_resolve_default_model_follows_provider_default_openrouter(monkeypatch):
    monkeypatch.delenv("SOME_MODEL", raising=False)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "openrouter")
    from llm_providers.models import resolve_default_model
    assert resolve_default_model("SOME_MODEL", tier="fast") == "meta-llama/llama-3.3-70b-instruct:free"
    assert resolve_default_model("SOME_MODEL", tier="quality") == "deepseek/deepseek-chat-v3.1:free"


def test_resolve_default_model_follows_provider_default_bedrock(monkeypatch):
    monkeypatch.delenv("SOME_MODEL", raising=False)
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "bedrock")
    from llm_providers.models import resolve_default_model
    fast = resolve_default_model("SOME_MODEL", tier="fast")
    quality = resolve_default_model("SOME_MODEL", tier="quality")
    assert "claude-haiku" in fast
    assert "claude-sonnet" in quality
