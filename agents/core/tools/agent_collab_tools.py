"""Agent-to-agent and agent-to-BI-agent delegation tool implementations.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime
from _shared import _hub_db_query

def communicate_with_agent(agent_id: str, message: str, **kwargs) -> dict:
    """Delegate a message to another hub agent and return that agent's final answer.

    Resolves the target agent by UUID or exact name, optionally enforces
    an allowlist of permitted target agents (configured per-agent via the
    ``agent_ids`` tool config), then runs the target agent synchronously
    via ``agents_hub_bp.HubOrchestrator`` and collects its streamed
    response chunks into a single answer string. Token usage reported by
    the sub-agent's final chunk is accumulated and returned under
    underscore-prefixed keys for the calling orchestrator to bill against
    the parent conversation (these are not meant to be shown to the LLM).

    Args:
        agent_id (str): Target agent's UUID or exact name.
        message (str): Message/question to send to the target agent.
        **kwargs: Orchestrator-injected context. Recognized keys:
            ``api_key`` (str) — LLM API key forwarded to the sub-agent's
            orchestrator; ``_hub_ctx`` (dict) — hub session context merged
            into the sub-agent's context; ``agent_ids`` (str | list) —
            allowlist of agent UUIDs this caller may target.

    Returns:
        dict: On success, ``{"success": True, "agent_id": str,
        "agent_name": str, "message_sent": str, "response": str,
        "_tokens_used": int, "_input_tokens": int, "_output_tokens":
        int}``. On failure where the target/blueprint can't be resolved,
        either raises ``ValueError`` (agent not found, or not in the
        allowed list — the message lists valid names so the calling LLM
        can retry immediately) or returns ``{"error": str, "agent_id":
        str}`` (hub blueprint not loaded yet). Unexpected exceptions
        during the sub-agent run are caught and returned as
        ``{"success": False, "error": str, "agent_id": str}``.

    Raises:
        ValueError: If ``agent_id`` doesn't match any agent, or matches an
            agent outside the configured ``agent_ids`` allowlist.
    """
    api_key = kwargs.get('api_key', '')
    hub_ctx = kwargs.get('_hub_ctx', {})

    # Look up the target agent first (by UUID or name)
    rows = _hub_db_query(
        "SELECT * FROM hub_agents WHERE id=? OR name=?",
        (agent_id, agent_id), fetchall=True
    )
    if not rows:
        # Give the LLM the exact names it must choose from so it can retry immediately
        all_agents = _hub_db_query(
            "SELECT id, name FROM hub_agents WHERE status='active' OR status IS NULL",
            fetchall=True
        ) or []
        available = [f"{a['name']}" for a in all_agents[:10]]
        raise ValueError(
            f"Agent '{agent_id}' not found. "
            f"You MUST use one of these exact names: {available}. "
            f"Retry communicate_with_agent with the correct name now."
        )

    agent_row = rows[0]

    # Enforce allowed agents if configured — check by UUID after resolving name
    allowed_ids_raw = kwargs.get('agent_ids', [])
    if isinstance(allowed_ids_raw, str):
        allowed_ids = [a.strip() for a in allowed_ids_raw.split(',') if a.strip()]
    elif isinstance(allowed_ids_raw, list):
        allowed_ids = [str(a) for a in allowed_ids_raw if a]
    else:
        allowed_ids = []
    resolved_id = str(agent_row.get('id', ''))
    if allowed_ids and resolved_id not in allowed_ids:
        allowed_rows = _hub_db_query(
            f"SELECT name FROM hub_agents WHERE id IN ({','.join(['?']*len(allowed_ids))})",
            tuple(allowed_ids), fetchall=True
        ) or []
        allowed_names = [r['name'] for r in allowed_rows]
        raise ValueError(
            f"Agent '{agent_id}' is not allowed. "
            f"You MUST use one of these exact names: {allowed_names or allowed_ids}. "
            f"Retry communicate_with_agent with the correct name now."
        )
    # Parse tools_json
    agent_row['tools'] = json.loads(agent_row.get('tools_json') or '[]')

    # Build orchestrator context for the sub-agent
    sub_ctx = {
        **hub_ctx,
        'agent_id':   agent_row.get('id'),
        'agent_name': agent_row.get('name'),
    }

    try:
        hub_bp = sys.modules.get('agents_hub_bp')
        if not hub_bp:
            return {"error": "Hub blueprint not loaded yet.", "agent_id": agent_id}

        orch   = hub_bp.HubOrchestrator(api_key, sub_ctx)
        chunks = list(orch.run(message, agent_row, []))

        final_answer  = ""
        _sub_in_tok   = 0
        _sub_out_tok  = 0
        _sub_tot_tok  = 0
        for chunk in chunks:
            try:
                data = json.loads(chunk) if isinstance(chunk, str) else chunk
                if isinstance(data, dict):
                    t = data.get('type', '')
                    if t in ('answer', 'complete', 'final'):
                        final_answer  = data.get('content', final_answer)
                        # Capture token usage reported by the sub-agent's final chunk
                        _sub_in_tok   = int(data.get('input_tokens',  0) or 0)
                        _sub_out_tok  = int(data.get('output_tokens', 0) or 0)
                        _sub_tot_tok  = int(data.get('tokens_used',   0) or 0) or (_sub_in_tok + _sub_out_tok)
                    elif t == 'text':
                        final_answer += data.get('content', '')
            except Exception:
                pass

        return {
            "success":        True,
            "agent_id":       agent_row.get('id'),
            "agent_name":     agent_row.get('name'),
            "message_sent":   message,
            "response":       final_answer or f"Agent '{agent_row.get('name')}' processed your message.",
            # Private token fields — consumed by the calling orchestrator, not forwarded to the LLM
            "_tokens_used":   _sub_tot_tok,
            "_input_tokens":  _sub_in_tok,
            "_output_tokens": _sub_out_tok,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "agent_id": agent_id}


def communicate_with_data_agent(agent_name: str, question: str, **kwargs) -> dict:
    """Delegate a natural-language question to a BI (data) agent and return its answer.

    Resolves the BI agent from the running app's ``agent_manager``, applies
    several layers of access control, then runs the question through
    ``nlq_engine.process_question`` against that agent's configured
    database connection.

    Access control / guardrail layers applied, in order:

    1. ``agent_names`` tool config (if set) restricts which BI agent names
       this caller may target at all.
    2. For non-admin/non-dev users, the allowlist is further intersected
       with the user's individually assigned BI agents
       (``auth.get_assigned_agents``).
    3. Any per-user/per-agent guardrail configured via ``org_db`` (mandatory
       WHERE filters, restricted table list, custom instructions, and an
       identity/department/project context line) is prepended to the
       question text sent to the BI agent, so the BI agent's own SQL
       generation is constrained without the calling LLM needing to know
       about it.

    Token usage reported by the NLQ engine (which may make its own LLM
    calls to generate/explain SQL) is accumulated and returned under
    underscore-prefixed keys for the calling orchestrator to bill against
    the parent conversation.

    Args:
        agent_name (str): Exact name of the BI data agent to query.
        question (str): Natural-language question for the agent to answer.
        **kwargs: Orchestrator-injected context. Recognized keys:
            ``agent_names`` (str | list) — allowlist of BI agent names this
            caller may target; ``_hub_ctx`` (dict) — hub session context,
            used to resolve the calling user's id/role/email for guardrail
            and permission checks.

    Returns:
        dict: On success, ``{"success": bool, "agent_name": str,
        "question": str, "answer": str, "data": Any, "columns": Any,
        "sql": str | None, "_tokens_used": int, "_input_tokens": int,
        "_output_tokens": int}`` (``answer`` is the first non-empty of the
        engine's answer/response/message/sql_response fields). On
        failure inside the NLQ call, ``{"success": False, "error": str,
        "agent_name": str}``.

    Raises:
        RuntimeError: If the app module, or its ``agent_manager``/
            ``nlq_engine`` attributes, aren't initialised yet.
        ValueError: If ``agent_name`` doesn't resolve to a configured BI
            agent, isn't in the configured ``agent_names`` allowlist, or
            isn't assigned to the calling non-admin/non-dev user.
    """
    import uuid as _uuid

    # Resolve agent_manager and nlq_engine from the running app module
    app_mod = sys.modules.get("app") or sys.modules.get("__main__")
    if not app_mod:
        raise RuntimeError("App module not loaded — cannot reach BI agent system.")

    agent_manager_obj = getattr(app_mod, "agent_manager", None)
    nlq_engine_obj    = getattr(app_mod, "nlq_engine", None)
    if not agent_manager_obj or not nlq_engine_obj:
        raise RuntimeError("agent_manager or nlq_engine not initialised in app.")

    # List all available BI agents so errors are actionable
    all_agents = agent_manager_obj.load_agents() or []
    available_names = [a.get("name") for a in all_agents if a.get("name")]

    # Enforce allowed list when configured (agent_names arrives via HubExecutor tool_config merge)
    allowed_raw = kwargs.get("agent_names", [])
    if isinstance(allowed_raw, str):
        allowed_bi = [n.strip() for n in allowed_raw.split(',') if n.strip()]
    elif isinstance(allowed_raw, list):
        allowed_bi = [str(n) for n in allowed_raw if n]
    else:
        allowed_bi = []
    if allowed_bi and agent_name not in allowed_bi:
        raise ValueError(
            f"BI agent '{agent_name}' is not allowed. "
            f"You MUST use one of these exact names: {allowed_bi}. "
            f"Retry communicate_with_data_agent with the correct name now."
        )

    agent_config = agent_manager_obj.get_agent(agent_name)
    if not agent_config:
        if allowed_bi:
            raise ValueError(
                f"BI agent '{agent_name}' not found. "
                f"You MUST use one of these exact names: {allowed_bi}. "
                f"Retry communicate_with_data_agent with the correct name now."
            )
        raise ValueError(
            f"BI agent '{agent_name}' not found. "
            f"Available agents: {available_names}. "
            f"Retry with one of these exact names."
        )

    hub_ctx    = kwargs.get("_hub_ctx", {})
    user_ctx   = hub_ctx.get("user") or {}
    user_id    = user_ctx.get("id")
    user_role  = user_ctx.get("role", "user")
    session_id = str(_uuid.uuid4())

    # ── User-specific BI agent access enforcement ─────────────────────────────
    # For non-admin/dev users, restrict to their assigned BI agents only.
    if user_id and user_role not in ("admin", "dev"):
        try:
            _auth = sys.modules.get("auth")
            if _auth:
                user_assigned = _auth.get_assigned_agents(user_id)
                if not allowed_bi:
                    allowed_bi = user_assigned
                else:
                    allowed_bi = [a for a in allowed_bi if a in user_assigned]
                if allowed_bi and agent_name not in allowed_bi:
                    raise ValueError(
                        f"BI agent '{agent_name}' is not assigned to you. "
                        f"You may only use: {allowed_bi}."
                    )
        except ValueError:
            raise
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────────

    # ── Apply user-specific guardrail for this BI agent ───────────────────────
    if user_id:
        try:
            _org = sys.modules.get("org_db")
            if _org:
                _scope_type = hub_ctx.get("scope_type")
                _scope_id   = hub_ctx.get("scope_id")
                _guardrail  = _org.get_agent_guardrail(
                    user_id, agent_name, "bi",
                    active_scope_type=_scope_type,
                    active_scope_id=_scope_id,
                )
                _user_org  = _org.get_user_identity_context(user_id)
                _depts     = _user_org.get("departments", [])
                _projs     = _user_org.get("projects", [])
                _parts     = []
                _dept_str  = ", ".join(
                    f"{d['name']} ({d['role'].title()})" if d.get("role") else d["name"]
                    for d in _depts
                )
                _proj_str  = ", ".join(
                    f"{p['name']} ({p['role'].title()})" if p.get("role") else p["name"]
                    for p in _projs
                )
                _nexus_mod     = sys.modules.get("nexus_sync_db")
                _portal_uid    = _nexus_mod.get_portal_user_id(user_id) if _nexus_mod else None
                _effective_uid = _portal_uid if _portal_uid else user_id
                _uid_source    = "portal" if _portal_uid else "local"
                _id_line   = (
                    f"[Context: I am {user_ctx.get('username', '')} "
                    f"(role={user_role}, MY_USER_ID={_effective_uid}, id_source={_uid_source})"
                    + (f", department(s): {_dept_str}" if _depts else "")
                    + (f", project(s): {_proj_str}" if _projs else "")
                    + "]"
                )
                _parts.append(_id_line)
                if _guardrail:
                    _filters = _guardrail.get("filter_rules", [])
                    if _filters:
                        _f_str = " AND ".join(
                            f"{f.get('column')} {f.get('operator','=')} '{f.get('value','')}'"
                            for f in _filters
                        )
                        _parts.append(
                            f"[MANDATORY FILTER: ALWAYS apply WHERE {_f_str}. "
                            f"Never show data outside this filter.]"
                        )
                    _restrict = _guardrail.get("restrict_tables")
                    if _restrict:
                        _parts.append(f"[ALLOWED TABLES: {', '.join(_restrict)} only.]")
                    _custom = (_guardrail.get("custom_instruction") or "").strip()
                    if _custom:
                        _parts.append(f"[INSTRUCTION: {_custom}]")
                if _parts:
                    question = " ".join(_parts) + "\n\nUser question: " + question
                if _guardrail:
                    user_ctx = {**user_ctx, "_guardrail": _guardrail}
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────────

    # Accumulate tokens reported by the NLQ engine so the calling hub agent gets credit
    _da_tok_acc = {"total": 0, "input": 0, "output": 0}

    def _da_token_recorder(call_type: str, tokens: int,
                           input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate token usage reported by nlq_engine calls into `_da_tok_acc`."""
        _da_tok_acc["total"] += int(tokens or 0)
        _da_tok_acc["input"] += int(input_tokens or 0)
        _da_tok_acc["output"] += int(output_tokens or 0)

    try:
        result = nlq_engine_obj.process_question(
            question=question,
            agent_config=agent_config,
            connection_name=agent_config.get("database_connection", ""),
            session_id=session_id,
            user_context=user_ctx,
            extra_token_recorder=_da_token_recorder,
        )
        # Distil the response into something clean for the calling LLM
        answer = (
            result.get("answer")
            or result.get("response")
            or result.get("message")
            or result.get("sql_response")
            or ""
        )
        return {
            "success":    result.get("success", True),
            "agent_name": agent_name,
            "question":   question,
            "answer":     answer,
            "data":       result.get("data"),
            "columns":    result.get("columns"),
            "sql":        result.get("sql_query") or result.get("sql"),
            # Private token fields consumed by the calling orchestrator
            "_tokens_used":   _da_tok_acc["total"],
            "_input_tokens":  _da_tok_acc["input"],
            "_output_tokens": _da_tok_acc["output"],
        }
    except Exception as e:
        return {"success": False, "error": str(e), "agent_name": agent_name}
