"""agents/core/hub/orchestrator.py — HubOrchestrator, relocated from
blueprints/agents_hub_bp.py as part of the de-monolith (Phase 3 Slice 1).
"""
import sys
import agents_hub_bp as _hub_bp  # noqa: F401 — lazy attribute access (_hub_orch_mod)
import auth
from db_exec import run_query as _ss_exec  # noqa: F401
from core.hub.executor import HubExecutor


class HubOrchestrator:
    """Wraps ``core.orchestrator.engine.Orchestrator`` for hub chat/workflow runs.

    Note: this wraps an ``Orchestrator`` instance by composition
    (``self._orch``), it is not a subclass. Construction swaps the
    orchestrator's plain ``Executor`` for a ``HubExecutor`` so every tool
    call carries hub context and per-tool config, and ``run()`` additionally
    enriches the agent's system prompt with the allowed agent/BI-agent name
    lists before delegating to the wrapped orchestrator.
    """

    def __init__(self, api_key: str, hub_ctx: dict, tool_configs: dict = None):
        """Args:
            api_key: OpenAI API key passed through to the wrapped Orchestrator.
            hub_ctx: Hub request context (user, agent_id, agent_name, convo_id,
                agent_env_vars) forwarded to HubExecutor.
            tool_configs: Optional ``{tool_name: config_dict}`` map used both
                by HubExecutor and by ``_enrich_agent`` to find allowed
                agent/BI-agent names for communicate_with_agent tools.
        """
        Orchestrator = _hub_bp._hub_orch_mod.Orchestrator
        self._orch        = Orchestrator(api_key)
        self._orch.executor = HubExecutor(api_key, hub_ctx, tool_configs)
        self._tool_configs = tool_configs or {}

    def run(self, user_input, agent, history=None):
        """Run one agent turn, enriching the prompt unless it's small talk.

        Args:
            user_input: The user's message for this turn.
            agent: Agent record dict (name, objective, system_prompt, model, tools).
            history: Prior conversation turns, same shape as
                ``Orchestrator.run``'s ``conversation_history``.

        Returns:
            The NDJSON generator from the wrapped ``Orchestrator.run()``.
        """
        # Greetings/small talk skip the allowed-agent-name-list injection too —
        # there's nothing to route, so don't pay for it.
        if not _hub_bp._hub_orch_mod._is_small_talk(user_input):
            agent = self._enrich_agent(agent)
        return self._orch.run(user_input, agent, history)

    def _enrich_agent(self, agent: dict) -> dict:
        """Prepend allowed agent names+descriptions to system prompt so LLM never guesses."""
        notes = []

        # ── communicate_with_agent ───────────────────────────────────────────
        ca_cfg = self._tool_configs.get('communicate_with_agent', {})
        allowed_raw = ca_cfg.get('agent_ids', [])
        if isinstance(allowed_raw, str):
            allowed_ids = [a.strip() for a in allowed_raw.split(',') if a.strip()]
        elif isinstance(allowed_raw, list):
            allowed_ids = [str(a) for a in allowed_raw if a]
        else:
            allowed_ids = []
        if allowed_ids:
            try:
                placeholders = ','.join(['?'] * len(allowed_ids))
                rows = _ss_exec(
                    f"SELECT id, name, description FROM hub_agents WHERE id IN ({placeholders})",
                    tuple(allowed_ids), fetchall=True) or []
                if rows:
                    lines = []
                    for r in rows:
                        desc = (r.get('description') or '').strip()[:60]
                        lines.append(f'  - "{r["name"]}"{(" — " + desc) if desc else ""}')
                    notes.append(
                        "IMPORTANT — when using communicate_with_agent you MUST use one of these "
                        "exact agent names as agent_id (do NOT invent names):\n"
                        + '\n'.join(lines)
                    )
            except Exception:
                pass

        # ── communicate_with_data_agent ──────────────────────────────────────
        da_cfg = self._tool_configs.get('communicate_with_data_agent', {})
        allowed_bi_raw = da_cfg.get('agent_names', [])
        if isinstance(allowed_bi_raw, str):
            allowed_bi = [a.strip() for a in allowed_bi_raw.split(',') if a.strip()]
        elif isinstance(allowed_bi_raw, list):
            allowed_bi = [str(a) for a in allowed_bi_raw if a]
        else:
            allowed_bi = []
        try:
            app_mod = sys.modules.get('app') or sys.modules.get('__main__')
            agent_manager_obj = getattr(app_mod, 'agent_manager', None)
            if agent_manager_obj:
                all_bi = agent_manager_obj.load_agents() or []

                # Filter by tool config's allowed list first
                if allowed_bi:
                    all_bi = [a for a in all_bi if a.get('name') in allowed_bi]

                # For non-admin/dev users, further restrict to their assigned BI agents
                hub_user     = self._hub_ctx.get('user') or {}
                hub_user_id  = hub_user.get('id')
                hub_user_role = hub_user.get('role', 'user')
                if hub_user_id and hub_user_role not in ('admin', 'dev'):
                    try:
                        user_assigned = auth.get_assigned_agents(hub_user_id)
                        all_bi = [a for a in all_bi if a.get('name') in user_assigned]
                    except Exception:
                        pass

                if all_bi:
                    lines = []
                    for a in all_bi:
                        desc = (a.get('description') or '').strip()[:60]
                        lines.append(f'  - "{a["name"]}"{(" — " + desc) if desc else ""}')
                    notes.append(
                        "IMPORTANT — when using communicate_with_data_agent you MUST use one of these "
                        "exact BI agent names as agent_name (do NOT invent names):\n"
                        + '\n'.join(lines)
                    )
        except Exception:
            pass

        if not notes:
            return agent
        agent = dict(agent)
        agent['system_prompt'] = '\n\n'.join(notes) + '\n\n' + (agent.get('system_prompt') or '')
        return agent
