"""LangChain chat-model factory for subsystems built on LangChain.

Currently only ``nlq/nlq_engine.py``'s NL→SQL engine uses this — it's built
on ``langchain.chains.create_sql_query_chain``, which is deeply coupled to
LangChain's own ``BaseChatModel`` interface (not this package's
``LLMProvider``/``ChatResult`` shapes). Rather than rewrite that engine off
LangChain to fit ``llm_providers.base.LLMProvider``, this module leans on
LangChain's *own* multi-provider support: ``ChatOpenAI`` and
``langchain_aws.ChatBedrockConverse`` are both ``BaseChatModel``
implementations, so swapping between them is a drop-in replacement for
whatever code already calls ``.invoke()`` on the result — no changes needed
to the SQL-generation chain itself.

Kept inside ``llm_providers/`` so provider selection still lives in one
place, even though what's returned here is a LangChain object, not this
package's own chat interface.
"""
import os
from typing import Optional

from .errors import LLMConfigError


def get_langchain_chat_model(provider: Optional[str] = None, *, model: Optional[str] = None,
                              temperature: float = 0, timeout: int = 60, max_retries: int = 1,
                              api_key: Optional[str] = None):
    """Return a LangChain ``BaseChatModel`` for the resolved provider.

    Args:
        provider: "gemini" / "openrouter" / "bedrock" / None (falls back to
            ``DEFAULT_LLM_PROVIDER``). "openai" is parked (see comment below).
        model: Model id. None resolves via ``resolve_default_model("BEDROCK_NLQ_MODEL",
            tier="fast")`` — the "fast" tier for whichever provider is resolved,
            so this adapts automatically if ``DEFAULT_LLM_PROVIDER`` changes.
        temperature: Sampling temperature.
        timeout: Request timeout in seconds.
        max_retries: SDK-level retry count.
        api_key: Explicit API key override for the Gemini/OpenRouter path —
            ignored when the resolved provider is Bedrock.

    Raises:
        LLMConfigError: Unknown provider, or missing required configuration
            (``GEMINI_API_KEY`` / ``OPENROUTER_API_KEY`` / ``AWS_REGION``).
    """
    from .models import resolve_default_model

    resolved = (provider or os.getenv("DEFAULT_LLM_PROVIDER") or "gemini").strip().lower()
    model = model or resolve_default_model("BEDROCK_NLQ_MODEL", tier="fast")

    # OpenAI is parked for now — same reasoning as factory.py's _build().
    # if resolved == "openai":
    #     api_key = api_key or os.getenv("OPENAI_API_KEY")
    #     if not api_key:
    #         raise LLMConfigError("OPENAI_API_KEY is not set")
    #     from langchain_openai import ChatOpenAI
    #     return ChatOpenAI(
    #         model=model,
    #         temperature=temperature,
    #         openai_api_key=api_key,
    #         timeout=timeout,
    #         max_retries=max_retries,
    #     )

    if resolved in ("gemini", "openrouter"):
        # Both speak the OpenAI chat-completions protocol, so LangChain's
        # ChatOpenAI works for either — just point it at the right base_url
        # and key, same pattern as llm_providers/openai_compatible.py.
        from .gemini_provider import BASE_URL as GEMINI_BASE_URL
        from .openrouter_provider import BASE_URL as OPENROUTER_BASE_URL

        env_var = "GEMINI_API_KEY" if resolved == "gemini" else "OPENROUTER_API_KEY"
        base_url = GEMINI_BASE_URL if resolved == "gemini" else OPENROUTER_BASE_URL

        api_key = api_key or os.getenv(env_var)
        if not api_key:
            raise LLMConfigError(f"{env_var} is not set")
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            openai_api_key=api_key,
            openai_api_base=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    if resolved == "bedrock":
        region = os.getenv("AWS_REGION")
        if not region:
            raise LLMConfigError("AWS_REGION is not set (required for Amazon Bedrock)")
        from langchain_aws import ChatBedrockConverse
        # ChatBedrockConverse's boto3 session reads AWS_ACCESS_KEY_ID/
        # AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN from the environment itself
        # (standard boto3 behavior) — nothing to pass explicitly for those.
        # region_name IS passed explicitly: langchain_aws only auto-reads
        # AWS_DEFAULT_REGION, not this codebase's AWS_REGION.
        return ChatBedrockConverse(
            model=model,
            region_name=region,
            temperature=temperature,
        )

    raise LLMConfigError(f"Unknown LLM provider '{resolved}'")
