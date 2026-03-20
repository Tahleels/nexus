"""agents/core/hub/executor.py — HubExecutor, relocated from
blueprints/agents_hub_bp.py as part of the de-monolith (Phase 3 Slice 1).

Kept import-compatible with the original: agents_hub_bp.py still exposes
HubExecutor as a module attribute (via `from core.hub.executor import
HubExecutor`), which is what agents/core/tools/registry.py's
sys.modules['agents_hub_bp'].HubOrchestrator lookup and agents_hub_bp.py's
own internal use depend on.
"""
import agents_hub_bp as _hub_bp  # noqa: F401 — lazy attribute access only, see module docstring
from db_exec import run_query as _ss_exec  # noqa: F401 — used inside HubExecutor.execute


class HubExecutor:
    """Hub-aware tool executor.

    Note: this is a standalone class, not a subclass of
    ``core.orchestrator.engine.Executor`` — it independently implements the
    same ``execute(tool_name, params)`` interface so it can be swapped in as
    ``Orchestrator.executor`` (see ``HubOrchestrator``). On top of the base
    behavior, it also:

    - Injects ``_hub_ctx`` (current user, agent ID/name, conversation ID,
      agent env vars) into every tool call.
    - Merges per-tool ``config`` from the agent's ``tools_json`` (e.g.
      curated ``document_ids``, allowed ``agent_ids``) under the runtime params.
    - Provides a ``_db_query`` callable so tools can query SQL Server directly.
    """

    def __init__(self, api_key: str, hub_ctx: dict, tool_configs: dict = None):
        """Args:
            api_key: OpenAI API key forwarded to tool calls (e.g. web_search).
            hub_ctx: Hub request context (user, agent_id, agent_name, convo_id,
                agent_env_vars) injected into every tool call as ``_hub_ctx``.
            tool_configs: Optional ``{tool_name: config_dict}`` map of
                per-tool configuration pulled from the agent's tool list.
        """
        self.api_key       = api_key
        self._hub_ctx      = hub_ctx
        self._tool_configs = tool_configs or {}

    def execute(self, tool_name: str, params: dict) -> dict:
        """Execute one tool call with hub context and per-tool config merged in.

        Args:
            tool_name: Name of the tool to run, as registered in TOOL_REGISTRY.
            params: Arguments parsed from the LLM's tool-call request; these
                take precedence over any matching keys in the tool's config.

        Returns:
            The tool's result dict, with an added ``_execution_time`` (seconds).
        """
        import time as _t
        from core.tools.registry import execute_tool

        # Provide a db_query helper so tools (e.g. search_documents) can hit SQL Server
        def _db_query(sql, qparams=None, fetchall=False, fetchone=False):
            return _ss_exec(sql, *([qparams] if qparams else []),
                            fetchall=fetchall, fetchone=fetchone)

        # Inject per-tool config before model params (model params take precedence,
        # except an empty/falsy LLM-supplied value never blanks out an admin-pinned
        # config value — e.g. connector_keys=[] from the model must not erase a
        # configured connector_keys=['sharepoint:5']).
        tool_config = self._tool_configs.get(tool_name, {})
        effective_params = dict(params)
        for k, v in tool_config.items():
            if k in effective_params and not effective_params[k] and v:
                effective_params.pop(k)

        start  = _t.time()
        result = execute_tool(tool_name, {
            **tool_config,
            **effective_params,
            'api_key':   self.api_key,
            '_hub_ctx':  self._hub_ctx,
            '_db_query': _db_query,
        })
        result['_execution_time'] = round(_t.time() - start, 3)
        return result
