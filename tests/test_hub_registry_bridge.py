"""Protects agents_hub_bp.py's cross-module string lookups — external code
reaches these off sys.modules.get('agents_hub_bp') by name:

    HubOrchestrator, HubExecutor, HubWorkflowEngine  (agents/core/hub/*.py,
        agents/core/tools/agent_collab_tools.py, tests)
    _exec_custom_tool  (agents/core/tools/registry.py, this blueprint's own
        tool-jobs routes)
    _hub_orch_mod, _fix_row, _APPROVAL_ENABLED, _ensure_approval_tool,
    _extract_tool_configs, _tool_names  (agents/core/hub/orchestrator.py,
        agents/core/hub/workflow_engine.py)

Phase 3 Slice 4 split agents_hub_bp.py's 85 routes into 10 files in
blueprints/, but all 9 names above must stay defined directly in
agents_hub_bp.py itself (not moved into a route file) since none of them
are route-handler internals — they're module-level helpers/classes that
external code depends on regardless of which routes get relocated.
"""

EXTERNALLY_REACHED_NAMES = (
    "HubExecutor", "HubOrchestrator", "HubWorkflowEngine",
    "_exec_custom_tool", "_hub_orch_mod", "_fix_row",
    "_APPROVAL_ENABLED", "_ensure_approval_tool", "_extract_tool_configs",
    "_tool_names",
)


def test_agents_hub_bp_in_sys_modules(flask_app):
    import sys
    assert "agents_hub_bp" in sys.modules


def test_hub_orchestrator_resolves_via_sys_modules(flask_app):
    import sys
    hub_bp = sys.modules.get("agents_hub_bp")
    assert hub_bp is not None
    assert hasattr(hub_bp, "HubOrchestrator")
    assert callable(hub_bp.HubOrchestrator)


def test_exec_custom_tool_resolves_via_sys_modules(flask_app):
    import sys
    hub_bp = sys.modules.get("agents_hub_bp")
    assert hub_bp is not None
    assert hasattr(hub_bp, "_exec_custom_tool")
    assert callable(hub_bp._exec_custom_tool)


def test_all_externally_reached_names_resolve(flask_app):
    import sys
    hub_bp = sys.modules.get("agents_hub_bp")
    assert hub_bp is not None
    for name in EXTERNALLY_REACHED_NAMES:
        assert hasattr(hub_bp, name), f"agents_hub_bp missing externally-reached name: {name}"


def test_agents_hub_bp_route_count_sane(flask_app):
    """Guards Phase 3 Slice 4 (agents_hub_bp.py's 85 routes moved into
    blueprints/agents_hub_*_routes.py, registered onto the SAME Blueprint
    object via plain side-effect imports at the bottom of agents_hub_bp.py)
    — catches a mass-deletion or a file silently failing to import."""
    import sys
    hub_bp = sys.modules.get("agents_hub_bp")
    bp = hub_bp.agents_hub_bp
    assert len(bp.deferred_functions) >= 80, (
        f"only {len(bp.deferred_functions)} routes registered on agents_hub_bp, expected ~85"
    )
