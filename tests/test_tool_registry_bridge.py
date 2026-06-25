"""Protects agents/core/tools/registry.py's two independent load identities
(Phase 3 Slice 3 split registry.py's ~3,751 lines into flat sibling modules
in agents/core/tools/ — db_tools.py, graph_tools.py, etc. — imported back
into registry.py via plain sys.path-based bare imports, deliberately not a
subpackage, to avoid touching the _PRELOAD spec_from_file_location loader):

    1. 'core.tools.registry'        — blueprints/agents_hub_bp.py's _PRELOAD
    2. 'agents.core.tools.registry' — services/workspace_openai_service.py's
                                       normal dotted-package import

Both must resolve TOOL_REGISTRY/execute_tool identically, and the dict-value
shape each TOOL_REGISTRY entry exposes (schema/display_name/description/
category/callable function) must be unchanged, since agents_hub_bp.py reads
those keys directly.
"""
import sys


def test_preload_identity_resolves_tool_registry(flask_app):
    reg = sys.modules.get("core.tools.registry")
    assert reg is not None
    assert hasattr(reg, "TOOL_REGISTRY")
    assert hasattr(reg, "execute_tool")
    assert callable(reg.execute_tool)
    assert len(reg.TOOL_REGISTRY) >= 20


def test_dotted_identity_resolves_tool_registry(flask_app):
    import importlib
    mod = importlib.import_module("agents.core.tools.registry")
    assert hasattr(mod, "TOOL_REGISTRY")
    assert len(mod.TOOL_REGISTRY) >= 20


def test_dotted_identity_direct_function_imports(flask_app):
    from agents.core.tools.registry import (
        web_search, get_teams_chats_with_person, get_outlook_emails,
    )
    assert callable(web_search)
    assert callable(get_teams_chats_with_person)
    assert callable(get_outlook_emails)


def test_tool_registry_entry_shape_preserved(flask_app):
    reg = sys.modules.get("core.tools.registry")
    for name in ("web_search", "query_database", "check_system_status"):
        entry = reg.TOOL_REGISTRY[name]
        for key in ("display_name", "category", "description", "function", "schema", "icon"):
            assert key in entry, f"{name} missing key {key}"
        assert callable(entry["function"])
