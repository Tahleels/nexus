"""Provider-agnostic LLM abstraction: Gemini + OpenRouter (both free-tier) as
the active providers, Amazon Bedrock parked pending AWS access, and OpenAI
parked in favor of the free providers — all without touching call sites
(see ``base.LLMProvider``).

Deliberately a plain top-level package (``llm_providers``, not nested under
``core/``) — this codebase already has two unrelated directories both named
``core`` on ``sys.path`` at different times (the project-root ``core/``
added by ``app.py`` for flat imports like ``import auth``, and
``agents/core/`` added by ``blueprints/agents_hub_bp.py`` and imported as a
real package via ``from core.tools.registry import ...``). Nesting under
either would risk resolving to the wrong one depending on import order.

Usage — get a configured provider and make a call::

    from llm_providers.factory import get_provider
    provider = get_provider("gemini")  # or "openrouter"/"bedrock", or None for the configured default
    result = provider.chat(system=system_prompt, messages=messages, model=model_id)
"""
