# =============================================================
# workspace_openai_service.py  —  Workspace chat streaming service
# =============================================================
# Handles streaming chat, tool-calling (web search via the shared
# agents.core.tools.registry.web_search — not the Responses API hosted
# tool, see _stream_responses_api's note below), Microsoft Teams/Outlook
# tool-calling, and cost estimation for the Enterprise AI Workspace.
#
# The live path (_stream_with_function_tools / _stream_chat_completions)
# goes through llm_providers, so `provider` ("openai"/"bedrock") is a
# per-request/per-project choice, not hardcoded — see stream_workspace_chat().
# `_stream_responses_api` is OpenAI-Responses-API-specific (hosted
# web_search_preview tool, no Bedrock equivalent) and is currently dead code
# (no caller) since web_search was moved onto the shared tool registry
# instead — left as-is rather than migrated or removed.
#
# Public entry point: stream_workspace_chat(). Also exposes
# AVAILABLE_MODELS / MODEL_COSTS (consumed by workspace_bp.py and
# templates) and generate_conversation_title() (used by workspace_bp.py's
# background DB-flush thread to auto-name new conversations).
# =============================================================

import os
import time
from logging_config import get_logger

logger = get_logger(__name__)

# ── Cost table (per 1 K tokens, USD) ─────────────────────────────
MODEL_COSTS: dict[str, dict] = {
    "gpt-4o":           {"prompt": 0.0025,  "completion": 0.010},
    "gpt-4o-mini":      {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4.1":          {"prompt": 0.002,   "completion": 0.008},
    "gpt-4.1-mini":     {"prompt": 0.0004,  "completion": 0.0016},
    "gpt-4.1-nano":     {"prompt": 0.0001,  "completion": 0.0004},
    "o4-mini":          {"prompt": 0.0011,  "completion": 0.0044},
    "gpt-4o-search-preview": {"prompt": 0.0025, "completion": 0.010},
}

# Models that do not accept a temperature parameter
REASONING_MODELS = {"o4-mini"}

# Models that support image (vision) input
VISION_MODELS = {
    "gpt-4o", "gpt-4o-mini",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    "o4-mini",
}

AVAILABLE_MODELS = [
    # GPT-4o family
    {"id": "gpt-4o",        "label": "GPT-4o",        "supports_search": True,  "category": "GPT-4o",     "provider": "openai"},
    {"id": "gpt-4o-mini",   "label": "GPT-4o Mini",   "supports_search": True,  "category": "GPT-4o",     "provider": "openai"},
    # GPT-4.1 family (April 2025)
    {"id": "gpt-4.1",       "label": "GPT-4.1",       "supports_search": True,  "category": "GPT-4.1",    "provider": "openai"},
    {"id": "gpt-4.1-mini",  "label": "GPT-4.1 Mini",  "supports_search": True,  "category": "GPT-4.1",    "provider": "openai"},
    {"id": "gpt-4.1-nano",  "label": "GPT-4.1 Nano",  "supports_search": True,  "category": "GPT-4.1",    "provider": "openai"},
    # o-series reasoning
    {"id": "o4-mini",       "label": "o4-mini",        "supports_search": True,  "category": "Reasoning", "provider": "openai"},
    # Amazon Bedrock — web_search is a normal function-calling tool here (see
    # _DEFAULT_WS_TOOL_DEFS), not the OpenAI-only hosted Responses API tool,
    # so it works identically for these models too.
    {"id": "anthropic.claude-sonnet-5",                       "label": "Claude Sonnet 5",  "supports_search": True, "category": "Bedrock", "provider": "bedrock"},
    {"id": "anthropic.claude-opus-5",                         "label": "Claude Opus 5",    "supports_search": True, "category": "Bedrock", "provider": "bedrock"},
    {"id": "anthropic.claude-haiku-4-5-20251001-v1:0",        "label": "Claude Haiku 4.5", "supports_search": True, "category": "Bedrock", "provider": "bedrock"},
    {"id": "amazon.nova-2-lite-v1:0",                         "label": "Amazon Nova 2 Lite", "supports_search": True, "category": "Bedrock", "provider": "bedrock"},
]


def _get_client():
    """Construct a new OpenAI SDK client using the ``OPENAI_API_KEY`` env var."""
    from openai import OpenAI
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return USD cost using live pricing (falls back to local MODEL_COSTS table).

    Args:
        model: OpenAI model id (e.g. "gpt-4o").
        prompt_tokens: Number of prompt/input tokens consumed.
        completion_tokens: Number of completion/output tokens generated.

    Returns:
        Estimated cost in USD. Tries ``token_limits.compute_cost`` first (live
        pricing); falls back to the local ``MODEL_COSTS`` table (defaulting
        to the "gpt-4o" row if the model is unrecognised) if that import or
        call fails.
    """
    try:
        import token_limits as _tl
        return _tl.compute_cost(model, prompt_tokens, completion_tokens)
    except Exception:
        costs = MODEL_COSTS.get(model, MODEL_COSTS["gpt-4o"])
        return (
            (prompt_tokens    / 1000) * costs["prompt"]
            + (completion_tokens / 1000) * costs["completion"]
        )


# ─────────────────────────────────────────────────────────────
# DEFAULT TOOL DEFINITIONS  (web search, Teams, Outlook)
# ─────────────────────────────────────────────────────────────

_WEB_SEARCH_TOOL_DEF = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live internet for real-time information: current prices, "
                "news, recent events, or anything requiring up-to-date data. "
                "Call this whenever the user asks about current events, live prices, "
                "recent news, weather, stock prices, or anything you may not know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to look up on the internet",
                    },
                },
                "required": ["query"],
            },
        },
    }
]

_TEAMS_TOOL_DEF = [
    {
        "type": "function",
        "function": {
            "name": "get_teams_chats_with_person",
            "description": (
                "Read Microsoft Teams chat messages between you and another person. "
                "Call this whenever the user asks about Teams conversations, messages, "
                "chat history, or what was discussed with someone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Email address or display name of the person whose chat history to retrieve",
                    },
                    "pool_size": {
                        "type": "integer",
                        "description": "Number of messages to return (default 30)",
                    },
                    "include_groups": {
                        "type": "boolean",
                        "description": "Also search group chats (default false — 1:1 only)",
                    },
                    "group_name": {
                        "type": "string",
                        "description": "Filter to a specific group chat name",
                    },
                    "search_keyword": {
                        "type": "string",
                        "description": "Keyword to search for across message history",
                    },
                },
                "required": ["target"],
            },
        },
    }
]

_OUTLOOK_TOOL_DEF = [
    {
        "type": "function",
        "function": {
            "name": "get_outlook_emails",
            "description": (
                "Read emails from the current user's Outlook inbox via Microsoft Graph. "
                "Call this when the user asks about their emails, inbox, messages from someone, "
                "or anything related to Outlook email or mail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_address": {
                        "type": "string",
                        "description": "Filter by sender email address or display name",
                    },
                    "subject_keyword": {
                        "type": "string",
                        "description": "Search for this keyword in the email subject line",
                    },
                    "body_keyword": {
                        "type": "string",
                        "description": "Search for this keyword in the email body",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Mailbox folder: inbox (default), sent, drafts, deleted, archive, junk",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of emails to return (1-50, default 20)",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Set true to return only unread emails",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Start of date range as YYYY-MM-DD",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End of date range as YYYY-MM-DD",
                    },
                },
                "required": [],
            },
        },
    }
]

# All three tools active by default in every workspace chat
_DEFAULT_WS_TOOL_DEFS = _WEB_SEARCH_TOOL_DEF + _TEAMS_TOOL_DEF + _OUTLOOK_TOOL_DEF


def _execute_ws_tool(tool_name: str, args: dict, user_email: str) -> dict:
    """Execute a workspace tool call and return its result dict."""
    if tool_name == "web_search":
        try:
            from agents.core.tools.registry import web_search
            return web_search(query=args.get("query", ""))
        except Exception as exc:
            logger.exception("web_search execution failed")
            return {"success": False, "error": str(exc)}

    if tool_name == "get_teams_chats_with_person":
        try:
            from agents.core.tools.registry import get_teams_chats_with_person
            args = {k: v for k, v in args.items()}
            args["_hub_ctx"] = {"user": {"email": user_email}}
            return get_teams_chats_with_person(**args)
        except Exception as exc:
            logger.exception("Teams tool execution failed")
            return {"success": False, "error": str(exc)}

    if tool_name == "get_outlook_emails":
        try:
            from agents.core.tools.registry import get_outlook_emails
            args = {k: v for k, v in args.items()}
            args["_hub_ctx"] = {"user": {"email": user_email}}
            return get_outlook_emails(**args)
        except Exception as exc:
            logger.exception("Outlook tool execution failed")
            return {"success": False, "error": str(exc)}

    # All other configurable registry tools — delegate to registry by name
    if tool_name in _REGISTRY_TOOL_SCHEMAS:
        try:
            from agents.core.tools import registry as _reg
            fn = getattr(_reg, tool_name, None)
            if fn:
                return fn(**{k: v for k, v in args.items()})
        except Exception as exc:
            logger.exception("Registry tool %s failed", tool_name)
            return {"success": False, "error": str(exc)}

    return {"error": f"Unknown tool: {tool_name}"}


#: Keywords whose presence in the latest user message auto-enables web search
#: (when the selected model supports it and Teams chat is not active).
_SEARCH_KEYWORDS = {
    "latest", "news", "today", "current", "recent", "search the web",
    "browse", "real-time", "real time", "live", "now", "this week",
    "this month", "trending", "what happened", "search for",
}

_SEARCH_CAPABLE = {m["id"] for m in AVAILABLE_MODELS if m.get("supports_search")}


def _message_implies_search(messages: list[dict]) -> bool:
    """Check whether the latest user message contains a real-time/search-y keyword.

    Args:
        messages: Conversation history as ``[{"role": ..., "content": ...}, ...]``.

    Returns:
        True if the most recent ``role == "user"`` message contains any of
        ``_SEARCH_KEYWORDS`` (case-insensitive); False if no user message is
        found or none of the keywords match.
    """
    last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    lower = (last_user or "").lower()
    return any(kw in lower for kw in _SEARCH_KEYWORDS)


def stream_workspace_chat(
    messages: list[dict],          # [{role, content}, ...]
    model: str = "gpt-4o",
    provider: str = None,          # "openai" / "bedrock" / None -> configured default
    tools_enabled: list = None,
    system_prompt: str = None,
    temperature: float = 0.7,
    images: list = None,           # [{name, mimeType, dataUrl}] from file attachments
    user_email: str = None,        # session user's email — required for Teams/Outlook tools
    extra_tool_defs: list = None,  # additional OpenAI function defs from project config
):
    """Public entry point — generator yielding streaming chat event dicts.

    Always uses function calling with the three default tools (web_search,
    Teams, Outlook) plus any project-specific ``extra_tool_defs``.

    Yields:
        {"type": "chunk", "text": "..."}
        {"type": "tool_start", "tool": "...", "query": "..."}
        {"type": "tool_done",  "tool": "..."}
        {"type": "done",  "full_text": "...", "model": "...",
                          "prompt_tokens": N, "completion_tokens": N,
                          "total_tokens": N, "latency_ms": N,
                          "estimated_cost": F, "citations": [...]}
        {"type": "error", "message": "..."}
    """
    tool_defs = _DEFAULT_WS_TOOL_DEFS + (extra_tool_defs or [])
    try:
        yield from _stream_with_function_tools(
            messages, model, system_prompt, temperature, images,
            user_email=user_email or "",
            tool_defs=tool_defs,
            provider=provider,
        )
    except Exception as exc:
        logger.exception("stream_workspace_chat top-level error")
        yield {"type": "error", "message": str(exc)}


# ─────────────────────────────────────────────────────────────
# FUNCTION CALLING  (Teams tool + any future workspace tools)
# ─────────────────────────────────────────────────────────────

def _stream_with_function_tools(messages, model, system_prompt, temperature, images, user_email,
                                 tool_defs=None, provider=None):
    """Stream a chat response with the default tools available via function calling.

    Prepends an instruction nudging the model to always call the live tools,
    then loops: stream a turn via ``llm_providers``, and if the model
    requests tool calls, execute them via ``_execute_ws_tool`` and feed the
    results back as ``role: "tool"`` messages, repeating until a turn comes
    back with no tool calls.

    Args:
        messages: Conversation history as ``[{"role", "content"}, ...]``.
        model: Model id for whichever provider is selected.
        system_prompt: Optional system prompt; combined with the tools
            instruction note.
        temperature: Sampling temperature (ignored for ``REASONING_MODELS``).
        images: Optional list of ``{name, mimeType, dataUrl}`` attachment
            dicts injected into the last user turn for vision-capable models.
        user_email: Session user's email passed through to
            ``_execute_ws_tool`` for Teams/Outlook tool calls.
        tool_defs: OpenAI function-calling tool definitions to offer.
        provider: "openai" / "bedrock" / None (falls back to the configured
            default — see ``llm_providers.factory.get_provider``).

    Yields:
        Same event shapes as ``stream_workspace_chat`` (chunk/tool_start/
        tool_done/done/error); citations in the final "done" event are
        always ``[]`` since this path does not use the Responses API.
    """
    import json as _json
    from llm_providers.factory import get_provider

    llm       = get_provider(provider)
    images    = images or []
    tool_defs = tool_defs or _DEFAULT_WS_TOOL_DEFS
    start     = time.time()
    full_text = ""
    prompt_tokens = completion_tokens = 0

    _tools_note = (
        "You have access to three live tools — use them without hesitation:\n"
        "• web_search: call for ANY real-time question (prices, news, current events, weather, etc.)\n"
        "• get_teams_chats_with_person: call when the user asks about Teams messages or chats\n"
        "• get_outlook_emails: call when the user asks about their emails or inbox\n"
        "Never say you cannot access real-time data or personal messages — use the tools."
    )
    effective_system = (
        f"{_tools_note}\n\n{system_prompt.strip()}" if (system_prompt and system_prompt.strip())
        else _tools_note
    )

    all_messages = []
    for i, m in enumerate(messages):
        is_last_user = (m["role"] == "user" and i == len(messages) - 1)
        if is_last_user and images and model in VISION_MODELS:
            content = [{"type": "text", "text": m["content"]}]
            for img in images:
                content.append({"type": "image_url", "image_url": {"url": img["dataUrl"], "detail": "auto"}})
            all_messages.append({"role": "user", "content": content})
        else:
            all_messages.append(m)

    effective_temperature = None if model in REASONING_MODELS else temperature

    # Loop: stream → detect tool_calls → execute → continue until text response
    while True:
        chunk_text   = ""
        final_result = None

        try:
            for delta in llm.stream_chat(system=effective_system, messages=all_messages, model=model,
                                          tools=tool_defs, temperature=effective_temperature):
                if delta.text:
                    chunk_text += delta.text
                    full_text  += delta.text
                    yield {"type": "chunk", "text": delta.text}
                if delta.done:
                    final_result = delta.result
        except Exception as exc:
            logger.exception("Function-calling stream failed")
            yield {"type": "error", "message": str(exc)}
            return

        tool_calls = final_result.tool_calls if final_result else []
        if final_result:
            prompt_tokens     += final_result.usage.prompt_tokens
            completion_tokens += final_result.usage.completion_tokens

        if not tool_calls:
            break   # done — final text already streamed

        # Append assistant turn with tool_calls (already OpenAI wire shape —
        # every provider's stream_chat() normalizes to this, see llm_providers/types.py)
        all_messages.append({
            "role":       "assistant",
            "content":    chunk_text or None,
            "tool_calls": tool_calls,
        })

        # Execute each tool and append results
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                args = _json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}

            tool_query = args.get("query") or args.get("target") or args.get("from_address") or ""
            yield {"type": "tool_start", "tool": fn_name, "query": tool_query}
            result = _execute_ws_tool(fn_name, args, user_email)
            yield {"type": "tool_done", "tool": fn_name}

            all_messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      _json.dumps(result) if isinstance(result, dict) else str(result),
            })
        # Loop back to let the model compose a final text response

    latency_ms = int((time.time() - start) * 1000)
    cost = estimate_cost(model, prompt_tokens, completion_tokens)
    yield {
        "type":               "done",
        "full_text":          full_text,
        "model":              model,
        "prompt_tokens":      prompt_tokens,
        "completion_tokens":  completion_tokens,
        "total_tokens":       prompt_tokens + completion_tokens,
        "latency_ms":         latency_ms,
        "estimated_cost":     cost,
        "citations":          [],
    }


# ─────────────────────────────────────────────────────────────
# CHAT COMPLETIONS  (no web search)
# ─────────────────────────────────────────────────────────────

def _stream_chat_completions(messages, model, system_prompt, temperature, images=None, provider=None):
    """Stream a plain chat response (no tools, no web search).

    Args:
        messages: Conversation history as ``[{"role", "content"}, ...]``.
        model: Model id for whichever provider is selected.
        system_prompt: Optional system prompt.
        temperature: Sampling temperature (ignored for ``REASONING_MODELS``).
        images: Optional list of ``{name, mimeType, dataUrl}`` attachment
            dicts injected into the last user turn for vision-capable models.
        provider: "openai" / "bedrock" / None (falls back to the configured
            default — see ``llm_providers.factory.get_provider``).

    Yields:
        ``{"type": "chunk", "text": ...}`` events while streaming, then a
        final ``{"type": "done", ...}`` event with usage/cost/citations
        (citations always ``[]``), or ``{"type": "error", "message": ...}``
        if the API call raises.
    """
    from llm_providers.factory import get_provider

    llm    = get_provider(provider)
    images = images or []

    all_messages = []
    for i, m in enumerate(messages):
        is_last_user = (m["role"] == "user" and i == len(messages) - 1)
        if is_last_user and images and model in VISION_MODELS:
            content = [{"type": "text", "text": m["content"]}]
            for img in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["dataUrl"], "detail": "auto"},
                })
            all_messages.append({"role": "user", "content": content})
        else:
            all_messages.append(m)

    start = time.time()
    full_text = ""
    prompt_tokens = 0
    completion_tokens = 0
    effective_temperature = None if model in REASONING_MODELS else temperature

    try:
        for delta in llm.stream_chat(system=system_prompt or "", messages=all_messages, model=model,
                                      temperature=effective_temperature):
            if delta.text:
                full_text += delta.text
                yield {"type": "chunk", "text": delta.text}
            if delta.done and delta.result:
                prompt_tokens     = delta.result.usage.prompt_tokens
                completion_tokens = delta.result.usage.completion_tokens
    except Exception as exc:
        logger.exception("Chat Completions streaming failed")
        yield {"type": "error", "message": str(exc)}
        return

    latency_ms = int((time.time() - start) * 1000)
    cost = estimate_cost(model, prompt_tokens, completion_tokens)

    yield {
        "type":               "done",
        "full_text":          full_text,
        "model":              model,
        "prompt_tokens":      prompt_tokens,
        "completion_tokens":  completion_tokens,
        "total_tokens":       prompt_tokens + completion_tokens,
        "latency_ms":         latency_ms,
        "estimated_cost":     cost,
        "citations":          [],
    }


# ─────────────────────────────────────────────────────────────
# RESPONSES API  (web_search_preview)
# ─────────────────────────────────────────────────────────────

def _stream_responses_api(messages, model, system_prompt, temperature, images=None):
    """Stream a response via the OpenAI Responses API with web_search_preview enabled.

    Converts the chat-style message list into Responses API ``input`` format,
    streams text deltas and web-search lifecycle events, then extracts URL
    citations and token usage from the final response object. If the
    Responses API call raises, falls back to ``_stream_chat_completions``
    (without web search) for the same messages/model/system_prompt/images.

    Args:
        messages: Conversation history as ``[{"role", "content"}, ...]``.
            Only ``"user"``/``"assistant"`` roles are forwarded.
        model: OpenAI model id to use.
        system_prompt: Optional instructions passed as ``kwargs["instructions"]``.
        temperature: Sampling temperature — accepted for signature parity
            with the other ``_stream_*`` helpers but not passed to the
            Responses API call (and reused as-is in the fallback call).
        images: Optional list of ``{name, mimeType, dataUrl}`` attachment
            dicts injected into the last user turn for vision-capable models.

    Yields:
        ``{"type": "chunk", ...}``, ``{"type": "tool_start"/"tool_done",
        "tool": "web_search", ...}`` events while streaming, then a final
        ``{"type": "done", ...}`` event including any ``url_citation``
        annotations found in the response output.
    """
    client = _get_client()
    images = images or []

    # Convert messages to Responses API input format
    input_msgs = []
    for i, m in enumerate(messages):
        if m.get("role") not in ("user", "assistant"):
            continue
        is_last_user = (m["role"] == "user" and i == len(messages) - 1)
        if is_last_user and images and model in VISION_MODELS:
            content = [{"type": "input_text", "text": m["content"]}]
            for img in images:
                content.append({"type": "input_image", "image_url": img["dataUrl"]})
            input_msgs.append({"role": "user", "content": content})
        else:
            input_msgs.append({"role": m["role"], "content": m["content"]})

    start = time.time()
    full_text = ""
    citations: list[dict] = []
    prompt_tokens = 0
    completion_tokens = 0

    kwargs: dict = {
        "model":  model,
        "input":  input_msgs,
        "tools":  [{"type": "web_search_preview"}],
    }
    if system_prompt and system_prompt.strip():
        kwargs["instructions"] = system_prompt

    try:
        with client.responses.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", None)

                if etype == "response.output_text.delta":
                    chunk = getattr(event, "delta", "")
                    if chunk:
                        full_text += chunk
                        yield {"type": "chunk", "text": chunk}

                elif etype == "response.web_search_call.searching":
                    query = getattr(event, "query", "")
                    yield {"type": "tool_start", "tool": "web_search", "query": query}

                elif etype == "response.web_search_call.completed":
                    yield {"type": "tool_done", "tool": "web_search"}

                elif etype in ("response.output_text.done", "response.done"):
                    pass  # handled below via get_final_response

            # Extract citations + usage from completed response
            try:
                final = stream.get_final_response()
                for item in (getattr(final, "output", None) or []):
                    for ann in (getattr(item, "annotations", None) or []):
                        if getattr(ann, "type", "") == "url_citation":
                            citations.append({
                                "url":     getattr(ann, "url",         ""),
                                "title":   getattr(ann, "title",       ""),
                                "snippet": getattr(ann, "quoted_text", "")[:500],
                            })
                usage = getattr(final, "usage", None)
                if usage:
                    prompt_tokens     = getattr(usage, "input_tokens",  0) or 0
                    completion_tokens = getattr(usage, "output_tokens", 0) or 0
            except Exception as parse_err:
                logger.warning(f"Could not parse final response: {parse_err}")

    except Exception as exc:
        logger.warning(f"Responses API failed ({exc}), falling back to Chat Completions")
        yield from _stream_chat_completions(messages, model, system_prompt, temperature, images)
        return

    latency_ms = int((time.time() - start) * 1000)
    cost = estimate_cost(model, prompt_tokens, completion_tokens)

    yield {
        "type":               "done",
        "full_text":          full_text,
        "model":              model,
        "prompt_tokens":      prompt_tokens,
        "completion_tokens":  completion_tokens,
        "total_tokens":       prompt_tokens + completion_tokens,
        "latency_ms":         latency_ms,
        "estimated_cost":     cost,
        "citations":          citations,
    }


# ─────────────────────────────────────────────────────────────
# REGISTRY TOOL BUILDER  (project-specific tools from TOOL_REGISTRY)
# ─────────────────────────────────────────────────────────────

# Maps every configurable registry tool name to its OpenAI function-calling schema.
_REGISTRY_TOOL_SCHEMAS = {
    # ── Data ──────────────────────────────────────────────────
    "query_database": {
        "description": "Execute a SQL SELECT query against a configured database and return the results.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql":             {"type": "string", "description": "SQL SELECT query to execute"},
                "connection_name": {"type": "string", "description": "Database connection name (uses first available if omitted)"},
            },
            "required": ["sql"],
        },
    },
    "update_company_watchlist": {
        "description": "Add or remove a company from the active company watchlist (News.CompanyNames). action='add' or 'remove'.",
        "parameters": {
            "type": "object",
            "properties": {
                "action":            {"type": "string", "description": "'add' or 'remove'"},
                "new_client":        {"type": "string", "description": "Company name to add or remove"},
                "reference_company": {"type": "string", "description": "Reference client name (pass 'None' if not found)"},
                "connection_name":   {"type": "string", "description": "Database connection name (optional)"},
            },
            "required": ["action", "new_client"],
        },
    },
    "analyze_csv_files": {
        "description": "Load CSV or Excel files and run pandas analysis. Use preview_schema=True first to inspect columns, then pass a 'code' string.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern":        {"type": "string",  "description": "Glob pattern to match filenames, e.g. 'GA_*.csv'"},
                "files":          {"type": "array",   "items": {"type": "string"}, "description": "Explicit list of filenames to load"},
                "preview_schema": {"type": "boolean", "description": "Return column names, dtypes and 5 sample rows without running code"},
                "code":           {"type": "string",  "description": "Pandas code to execute. 'df' = combined DataFrame. Assign result to a variable named 'result'."},
            },
            "required": [],
        },
    },
    "run_python_code": {
        "description": "Execute sandboxed Python code (json, math, re, datetime, collections, itertools, statistics, random allowed). Use print() to capture output.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
    },
    # ── File Operations ────────────────────────────────────────
    "create_text_file": {
        "description": "Create or overwrite a text file in the agent file store.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Filename (e.g. report.txt)"},
                "content":  {"type": "string", "description": "Text content to write"},
            },
            "required": ["filename", "content"],
        },
    },
    "load_text_file": {
        "description": "Read a text file previously created with create_text_file.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Filename to read (e.g. report.txt)"},
            },
            "required": ["filename"],
        },
    },
    "folder_report": {
        "description": "List contents and statistics for a directory. Leave path empty for the agent file store.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to inspect (leave empty for agent file store)"},
            },
            "required": [],
        },
    },
    # ── Communication ──────────────────────────────────────────
    "send_email": {
        "description": "Send an email via SMTP. Requires SMTP_HOST, SMTP_USER, SMTP_PASS env vars.",
        "parameters": {
            "type": "object",
            "properties": {
                "to":      {"type": "string",  "description": "Recipient email address"},
                "subject": {"type": "string",  "description": "Email subject line"},
                "body":    {"type": "string",  "description": "Email body (plain text by default)"},
                "cc":      {"type": "string",  "description": "CC email address (optional)"},
                "html":    {"type": "boolean", "description": "Set true to send body as HTML"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "send_sms": {
        "description": "Send an SMS via Twilio. Requires TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM env vars.",
        "parameters": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "description": "Recipient phone number in E.164 format (e.g. +14155552671)"},
                "message": {"type": "string", "description": "SMS message body (max 160 chars)"},
            },
            "required": ["to", "message"],
        },
    },
    # ── External Integration ───────────────────────────────────
    "call_external_api": {
        "description": "Make an HTTP request (GET/POST/PUT/DELETE) to any external REST API and return the response.",
        "parameters": {
            "type": "object",
            "properties": {
                "url":     {"type": "string", "description": "Full URL to call"},
                "method":  {"type": "string", "description": "HTTP method: GET, POST, PUT, DELETE (default GET)"},
                "payload": {"type": "object", "description": "JSON body for POST/PUT requests"},
                "headers": {"type": "object", "description": "Extra HTTP headers as key-value pairs"},
            },
            "required": ["url"],
        },
    },
    # ── Document Management ────────────────────────────────────
    "process_document": {
        "description": "Ingest a document file into the vector knowledge base (RAG) for future semantic search.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path":   {"type": "string", "description": "Server-side path to the file to ingest"},
                "source_name": {"type": "string", "description": "Display name for the document"},
            },
            "required": ["file_path"],
        },
    },
    "search_knowledge": {
        "description": "Retrieve relevant content from uploaded documents using semantic search (RAG). Call this before answering questions that may be covered by documents.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":     {"type": "string",  "description": "Natural-language question or keywords to search for"},
                "n_results": {"type": "integer", "description": "Maximum number of results to return (default 5)"},
            },
            "required": ["query"],
        },
    },
    "search_connector_knowledge": {
        "description": "Search documents ingested from a specific directory or SharePoint connector by semantic similarity.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":          {"type": "string",  "description": "Question or keywords to search for"},
                "n_results":      {"type": "integer", "description": "Maximum results to return (default 5)"},
                "connector_keys": {"type": "array",   "items": {"type": "string"}, "description": "Connector keys (e.g. ['filesystem:3','sharepoint:1'])"},
            },
            "required": ["query"],
        },
    },
    "list_connector_documents": {
        "description": "List documents ingested from a connector with their summaries. Use as Stage 1 to identify relevant files before deep search.",
        "parameters": {
            "type": "object",
            "properties": {
                "connector_keys": {"type": "array",   "items": {"type": "string"}, "description": "Connector keys"},
                "query":          {"type": "string",  "description": "Rank all documents by similarity to this text and return top matches"},
                "n_results":      {"type": "integer", "description": "Max documents to return when query is given (default 20)"},
            },
            "required": [],
        },
    },
    "list_knowledge_documents": {
        "description": "List directly-attached knowledge documents with their summaries. Use as Stage 1 to identify relevant files before deep search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":     {"type": "string",  "description": "Rank documents by similarity to this text"},
                "n_results": {"type": "integer", "description": "Max documents to return (default 20)"},
            },
            "required": [],
        },
    },
    "manage_knowledge": {
        "description": "Persistently store and retrieve key-value data across agent sessions (get/set/delete/list).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "delete", "list"], "description": "Action: get, set, delete, or list"},
                "key":    {"type": "string", "description": "Key name to get/set/delete"},
                "value":  {"type": "string", "description": "Value to store (required for 'set')"},
            },
            "required": ["action", "key"],
        },
    },
    # ── Agent Collaboration ────────────────────────────────────
    "communicate_with_agent": {
        "description": "Send a message to another hub agent and receive its response. Use agent UUID or exact agent name.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Target agent UUID or exact name"},
                "message":  {"type": "string", "description": "Message to send to the agent"},
            },
            "required": ["agent_id", "message"],
        },
    },
    "communicate_with_data_agent": {
        "description": "Send a natural-language question to a BI data agent that queries its connected database.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Exact name of the BI data agent to query"},
                "question":   {"type": "string", "description": "Natural-language question for the data agent"},
            },
            "required": ["agent_name", "question"],
        },
    },
    # ── System ─────────────────────────────────────────────────
    "check_system_status": {
        "description": "Get real-time system health metrics: CPU, memory, disk usage, active agents, uptime.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

# Default tools always active in every workspace chat (not shown in project tool config)
_DEFAULT_WS_TOOL_NAMES = {"web_search", "get_teams_chats_with_person", "get_outlook_emails"}

# All configurable tools shown in the admin project tool config modal
CONFIGURABLE_WS_TOOLS = [
    # Data
    {"id": "query_database",           "label": "Query Database",           "category": "Data",                 "icon": "fa-database"},
    {"id": "update_company_watchlist",  "label": "Update Company Watchlist", "category": "Data",                 "icon": "fa-list-check"},
    {"id": "analyze_csv_files",         "label": "Analyze CSV / Excel",      "category": "Data",                 "icon": "fa-file-excel"},
    {"id": "run_python_code",           "label": "Run Python Code",          "category": "Data",                 "icon": "fa-code"},
    # File Operations
    {"id": "create_text_file",          "label": "Create Text File",         "category": "File Operations",      "icon": "fa-file-alt"},
    {"id": "load_text_file",            "label": "Load Text File",           "category": "File Operations",      "icon": "fa-folder-open"},
    {"id": "folder_report",             "label": "Folder Report",            "category": "File Operations",      "icon": "fa-folder"},
    # Communication
    {"id": "send_email",                "label": "Send Email",               "category": "Communication",        "icon": "fa-envelope"},
    {"id": "send_sms",                  "label": "Send SMS",                 "category": "Communication",        "icon": "fa-sms"},
    # External Integration
    {"id": "call_external_api",         "label": "Call External API",        "category": "Integration",          "icon": "fa-link"},
    # Document Management
    {"id": "process_document",          "label": "Process Document (RAG)",   "category": "Document Management",  "icon": "fa-file-import"},
    {"id": "search_knowledge",          "label": "Search Knowledge Base",    "category": "Document Management",  "icon": "fa-search"},
    {"id": "search_connector_knowledge","label": "Search Connector Docs",    "category": "Document Management",  "icon": "fa-plug"},
    {"id": "list_connector_documents",  "label": "List Connector Documents", "category": "Document Management",  "icon": "fa-list"},
    {"id": "list_knowledge_documents",  "label": "List Knowledge Documents", "category": "Document Management",  "icon": "fa-list-alt"},
    {"id": "manage_knowledge",          "label": "Manage Knowledge (KV)",    "category": "Document Management",  "icon": "fa-brain"},
    # Agent Collaboration
    {"id": "communicate_with_agent",      "label": "Communicate with Agent",     "category": "Agent Collaboration", "icon": "fa-robot"},
    {"id": "communicate_with_data_agent", "label": "Communicate with Data Agent","category": "Agent Collaboration", "icon": "fa-chart-bar"},
    # System
    {"id": "check_system_status",       "label": "Check System Status",      "category": "System",               "icon": "fa-heartbeat"},
]


def _build_registry_tool_def(tool_name: str) -> dict | None:
    """Return an OpenAI function calling def for a registry tool, or None if unknown."""
    schema = _REGISTRY_TOOL_SCHEMAS.get(tool_name)
    if not schema:
        return None
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": schema["description"],
            "parameters": schema["parameters"],
        },
    }


# ─────────────────────────────────────────────────────────────
# AUTO-TITLE  (generate a short title for a new conversation)
# ─────────────────────────────────────────────────────────────

def generate_conversation_title(first_message: str) -> str:
    """Return a short (<= 7 word) title for a conversation from the first message.

    Uses a cheap/fast model via the configured default provider. Called from
    workspace_bp.py's background DB-flush thread the first time a
    conversation gets a reply — always the default provider rather than the
    conversation's own provider, since this is a system-level convenience
    feature, not a user-facing model choice. The model id adapts to
    ``DEFAULT_LLM_PROVIDER`` automatically (see
    ``llm_providers.models.resolve_default_model``); set
    ``WORKSPACE_TITLE_MODEL`` only to override that default explicitly.

    Args:
        first_message: The conversation's first user message (truncated to
            500 chars before sending to the model).

    Returns:
        A short title string. On API failure, falls back to the first 60
        characters of ``first_message``.
    """
    try:
        from llm_providers.factory import get_provider
        from llm_providers.models import resolve_default_model
        result = get_provider().chat(
            system="Generate a short, descriptive title (max 7 words, no quotes) "
                   "for a conversation that starts with the user message below. "
                   "Return only the title text.",
            messages=[{"role": "user", "content": first_message[:500]}],
            model=resolve_default_model("WORKSPACE_TITLE_MODEL", tier="fast"),
            max_tokens=30,
            temperature=0.3,
        )
        return (result.content or "").strip()
    except Exception as e:
        logger.error(f"generate_conversation_title failed: {e}")
        return first_message[:60].strip()
