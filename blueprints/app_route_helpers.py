"""blueprints/app_route_helpers.py — small pure helper functions used by the
app.py route modules (relocated from app.py during the Phase 3 Slice 2
route reorganization). No app.py singleton dependencies — safe to import
directly.
"""
import math
import nexus_sync_db


def clean_nan(obj):
    """Recursively replace float NaN values in dicts/lists with None so the result is valid JSON."""
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    elif isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


def _build_bi_guardrail_prefix(user: dict, user_org: dict, guardrail) -> str:
    """Build a context prefix injected before the user's BI question.

    Used by agent_chat() to prepend an identity line plus any guardrail-derived
    mandatory filters/table restrictions/custom instructions so the SQL-generating
    LLM always knows who is asking and what they are allowed to see.

    Args:
        user: Current user dict (id, username, role).
        user_org: Result of org_db.get_user_identity_context() — departments/projects.
        guardrail: Effective guardrail dict from org_db.get_agent_guardrail(), or None.

    Returns:
        A single-line bracketed context string, or '' if there is nothing to inject.
    """
    parts = []
    # User identity
    depts   = user_org.get('departments', [])
    projs   = user_org.get('projects', [])
    dept_str = ', '.join(
        f"{d['name']} ({d['role'].title()})" if d.get('role') else d['name']
        for d in depts
    )
    proj_str = ', '.join(
        f"{p['name']} ({p['role'].title()})" if p.get('role') else p['name']
        for p in projs
    )
    # Prefer the user's portal user id (if synced) as MY_USER_ID so BI
    # queries against portal-derived tables/views match correctly; fall back
    # to the local app user id when no portal mapping exists.
    portal_uid    = nexus_sync_db.get_portal_user_id(user.get('id'))
    effective_uid = portal_uid if portal_uid else user.get('id')
    uid_source    = 'portal' if portal_uid else 'local'
    id_line = (f"[Context: I am {user.get('username', '')} "
               f"(role={user.get('role', 'user')}, MY_USER_ID={effective_uid}, id_source={uid_source})"
               + (f", department(s): {dept_str}" if depts else '')
               + (f", project(s): {proj_str}" if projs else '')
               + "]")
    parts.append(id_line)
    # Guardrail filters
    if guardrail:
        filters = guardrail.get('filter_rules', [])
        if filters:
            filter_lines = ' AND '.join(
                f"{f.get('column')} {f.get('operator','=')} '{f.get('value','')}'"
                for f in filters
            )
            parts.append(
                f"[MANDATORY FILTER: ALWAYS apply WHERE {filter_lines} to every query. "
                f"Never show data outside this filter.]"
            )
        restrict = guardrail.get('restrict_tables')
        if restrict:
            parts.append(f"[ALLOWED TABLES: {', '.join(restrict)} only.]")
        custom = (guardrail.get('custom_instruction') or '').strip()
        if custom:
            parts.append(f"[INSTRUCTION: {custom}]")
    return ' '.join(parts) if parts else ''
