"""agents_hub_bp.py — Hub Agents Flask blueprint.

Integrates the agents/ sub-project into the main Flask app. All data is
stored in the app's SQL Server database, NOT SQLite. The agents/core/
directory is still used for the orchestrator and tools — this module loads
those submodules by file path (see import-path setup below) and wraps them
with hub-specific context (HubExecutor, HubOrchestrator, HubWorkflowEngine).

Access rules
------------
  user  → only Agents Chat (with their assigned agents)
  dev   → all pages + all API (all agents/workflows visible)
  admin → all pages + all API + assignment management

Routes
------
  GET  /agents-hub                              — Hub chat page
  GET  /preview/document                         — document preview page
  GET  /agents-hub/workflows                     — workflows page (dev/admin)
  GET  /agents-hub/agents                        — agents page (dev/admin)
  GET  /agents-hub/tools                         — tools page (dev/admin)
  GET  /agents-hub/knowledge                     — knowledge page (dev/admin)
  GET  /agents-hub/jobs                          — jobs page (dev/admin)
  GET  /agents-hub/agent-activity                — agent activity page (dev/admin)
  GET  /agents-hub/analytics                     — analytics page (dev/admin)
  GET  /agents-hub/dashboard                     — dashboard page (dev/admin)
  GET  /agents-hub/assignments                   — assignments page (dev/admin)
  GET  /agents-hub/custom-tools                  — custom tools page (dev/admin)
  GET  /agents-hub/tool-jobs                     — tool jobs page (dev/admin)
  GET  /agents-hub/tool-job-monitor              — tool job monitor page (dev/admin)
  GET  /approvals                                — approvals page (only if APPROVAL=true)

  GET  /api/agenthub/org/departments             — list departments (dev/admin)

  GET  /api/agenthub/agents                      — list agents visible to caller
  GET  /api/agenthub/agents/all                  — list all agents incl. inactive (dev/admin)
  GET  /api/agenthub/agents/<aid>                — get one agent
  POST /api/agenthub/agents                      — create agent (dev/admin)
  PUT  /api/agenthub/agents/<aid>                — update agent (dev/admin)
  DELETE /api/agenthub/agents/<aid>              — delete agent (dev/admin)
  POST /api/agenthub/agents/<aid>/add-doc        — attach a KB document to an agent's search_knowledge tool

  GET  /api/agenthub/chat/conversations          — list conversations
  POST /api/agenthub/chat/conversations          — create conversation
  GET  /api/agenthub/chat/conversations/<cid>    — get conversation + messages
  DELETE /api/agenthub/chat/conversations/<cid>  — delete conversation
  POST /api/agenthub/chat/stream                 — stream a chat turn (NDJSON)

  POST /api/agenthub/generate-document           — generate pptx/infographic/docx/csv/xlsx/markdown/txt/pdf from a conversation

  GET  /api/agenthub/workflows                   — list workflows visible to caller
  GET  /api/agenthub/workflows/<wid>             — get one workflow
  POST /api/agenthub/workflows                   — create workflow (dev/admin)
  PUT  /api/agenthub/workflows/<wid>             — update workflow (dev/admin)
  DELETE /api/agenthub/workflows/<wid>           — delete workflow (dev/admin)
  POST /api/agenthub/workflows/<wid>/run         — run a workflow (NDJSON stream)
  POST /api/agenthub/workflows/<wid>/resume      — resume a workflow paused at an approval node (NDJSON stream)
  GET  /api/agenthub/workflows/runs              — list recent workflow runs (dev/admin)

  GET  /api/agenthub/tools                       — list registry tools
  POST /api/agenthub/tools/<tid>/toggle          — enable/disable a tool (dev/admin)
  POST /api/agenthub/tools/test                  — test-run a tool (dev/admin)

  GET  /api/agenthub/knowledge                   — list legacy hub knowledge base entries
  POST /api/agenthub/knowledge                   — add a knowledge base entry (dev/admin)
  DELETE /api/agenthub/knowledge/<kid>           — delete a knowledge base entry (dev/admin)

  GET  /api/agenthub/jobs                        — list non-tool jobs (dev/admin)
  POST /api/agenthub/jobs                        — create job (dev/admin)
  POST /api/agenthub/jobs/<jid>/toggle           — pause/activate job (dev/admin)
  DELETE /api/agenthub/jobs/<jid>                — delete job (dev/admin)

  GET  /api/agenthub/analytics                   — agent/tool/token usage analytics (dev/admin)
  GET  /api/agenthub/dashboard/stats             — dashboard summary stats (dev/admin)

  GET  /api/agenthub/assignments/agents          — list agent assignments (admin)
  POST /api/agenthub/assignments/agents          — assign agent to user (admin)
  DELETE /api/agenthub/assignments/agents/<int:assignment_id>    — remove agent assignment (admin)
  GET  /api/agenthub/assignments/workflows       — list workflow assignments (admin)
  POST /api/agenthub/assignments/workflows       — assign workflow to user (admin)
  DELETE /api/agenthub/assignments/workflows/<int:assignment_id> — remove workflow assignment (admin)

  GET  /api/agenthub/admin/guardrails            — get scoped guardrail(s) (admin)
  PUT  /api/agenthub/admin/guardrails            — save scoped guardrail (admin)
  DELETE /api/agenthub/admin/guardrails          — delete scoped guardrail (admin)

  GET  /api/agenthub/approvals                   — list approvals visible to caller (HITL; APPROVAL=true)
  GET  /api/agenthub/approvals/pending-count      — pending approval count (HITL)
  GET  /api/agenthub/approvals/<approval_id>      — get one approval (HITL)
  POST /api/agenthub/approvals/<approval_id>/resolve — approve/reject (HITL)
  GET  /api/agenthub/approvals/<approval_id>/status  — poll approval status (HITL)

  GET  /api/agenthub/connections                  — list DB connections (no passwords) for workflow editor (dev/admin)
  GET  /api/agenthub/users                        — list users for approver selection (dev/admin)
  GET  /api/agenthub/my-access                    — caller's agent/workflow access summary

  GET  /api/agenthub/custom-tools                 — list custom tools (dev/admin)
  POST /api/agenthub/custom-tools                 — create custom tool (dev/admin)
  GET  /api/agenthub/custom-tools/<int:tid>       — get one custom tool (dev/admin)
  PUT  /api/agenthub/custom-tools/<int:tid>       — update custom tool (dev/admin)
  DELETE /api/agenthub/custom-tools/<int:tid>     — delete custom tool (dev/admin)
  POST /api/agenthub/custom-tools/<int:tid>/toggle      — enable/disable custom tool (dev/admin)
  POST /api/agenthub/custom-tools/<int:tid>/install     — create venv + install pip packages (dev/admin)
  GET  /api/agenthub/custom-tools/<int:tid>/venv-status — venv info (dev/admin)
  DELETE /api/agenthub/custom-tools/<int:tid>/venv      — delete the tool's venv (dev/admin)
  POST /api/agenthub/custom-tools/<int:tid>/test        — test-run a custom tool (dev/admin)

  GET  /api/agenthub/tool-jobs                    — list scheduled tool jobs (dev/admin)
  POST /api/agenthub/tool-jobs                    — create scheduled tool job (dev/admin)
  GET  /api/agenthub/tool-jobs/<jid>              — get one tool job (dev/admin)
  PUT  /api/agenthub/tool-jobs/<jid>              — update tool job (dev/admin)
  DELETE /api/agenthub/tool-jobs/<jid>            — delete tool job (dev/admin)
  POST /api/agenthub/tool-jobs/<jid>/toggle       — pause/activate tool job (dev/admin)
  POST /api/agenthub/tool-jobs/<jid>/run          — run tool job immediately, in background (dev/admin)
  GET  /api/agenthub/tool-jobs/<jid>/status       — last-run status of a tool job (dev/admin)
"""

import sys, os, uuid, json, logging, threading, re, calendar
from logging_config import get_logger
from datetime import datetime, timedelta, date
from flask import (Blueprint, render_template, jsonify, request,
                   Response, stream_with_context, redirect, url_for, abort)

import auth
import token_limits

# ── Org / guardrails — loaded lazily to avoid circular imports ────────────────
def _org():
    """Lazily import and return the org_db module (departments/projects/guardrails)."""
    import sys as _sys
    _db_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'database')
    if _db_dir not in _sys.path:
        _sys.path.insert(0, _db_dir)
    import org_db
    return org_db

# ── Add agents/core to import path ────────────────────────────────────────────
_AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'agents')
if _AGENTS_DIR not in sys.path:
    sys.path.insert(0, _AGENTS_DIR)

# Load orchestrator engine by file path to avoid collision with root core/ package.
# Flask caches sys.modules['core'] = root core/ before blueprints load, so
# "from core.orchestrator.engine" would fail — use importlib to bypass that.
import importlib.util as _ilu
_orch_file = os.path.join(_AGENTS_DIR, 'core', 'orchestrator', 'engine.py')
_orch_spec = _ilu.spec_from_file_location('_agents_hub_orch_engine', _orch_file)
_hub_orch_mod = _ilu.module_from_spec(_orch_spec)
_orch_spec.loader.exec_module(_hub_orch_mod)

# ── Pre-register agents/core submodules into sys.modules ──────────────────────
# app.py adds core/ (project-level) to sys.path, so 'from core.teams_crypto'
# below sets sys.modules['core'] to the project-level namespace package (no tools/).
# Pre-registering the full dotted names lets Python find them in sys.modules
# directly — it never needs to walk through the wrong parent 'core'.
_PRELOAD = [
    ('core.tools',            os.path.join(_AGENTS_DIR, 'core', 'tools',            '__init__.py')),
    ('core.tools.registry',   os.path.join(_AGENTS_DIR, 'core', 'tools',            'registry.py')),
    ('core.workflows',        os.path.join(_AGENTS_DIR, 'core', 'workflows',        '__init__.py')),
    ('core.workflows.engine', os.path.join(_AGENTS_DIR, 'core', 'workflows',        'engine.py')),
    ('core.knowledge',        os.path.join(_AGENTS_DIR, 'core', 'knowledge',        '__init__.py')),
    ('core.hub',              os.path.join(_AGENTS_DIR, 'core', 'hub',              '__init__.py')),
    ('core.hub.executor',     os.path.join(_AGENTS_DIR, 'core', 'hub',              'executor.py')),
    ('core.hub.orchestrator', os.path.join(_AGENTS_DIR, 'core', 'hub',              'orchestrator.py')),
    ('core.hub.workflow_engine', os.path.join(_AGENTS_DIR, 'core', 'hub',           'workflow_engine.py')),
]
for _mod_name, _mod_path in _PRELOAD:
    if _mod_name not in sys.modules and os.path.exists(_mod_path):
        _spec = _ilu.spec_from_file_location(_mod_name, _mod_path)
        _m    = _ilu.module_from_spec(_spec)
        sys.modules[_mod_name] = _m
        try:
            _spec.loader.exec_module(_m)
        except Exception as _pe:
            # Non-fatal — lazy imports will still find what's already cached
            sys.modules.pop(_mod_name, None)
del _PRELOAD, _mod_name, _mod_path, _spec, _m
# ─────────────────────────────────────────────────────────────────────────────

logger = get_logger(__name__)
agents_hub_bp = Blueprint('agents_hub', __name__)





# ═════════════════════════════════════════════════════════════════════════════
# SQL Server helper
# ═════════════════════════════════════════════════════════════════════════════

# _ss_exec — shared with agents/core/hub/*.py via database/db_exec.py
# (previously a locally-defined function here; now the single canonical copy)
from db_exec import run_query as _ss_exec


def _fix_row(row):
    """Stringify datetime fields so jsonify never chokes on them."""
    if not row:
        return row
    return {k: str(v) if hasattr(v, 'isoformat') else v for k, v in row.items()}


def _fix_rows(rows):
    return [_fix_row(r) for r in (rows or [])]


# ═════════════════════════════════════════════════════════════════════════════
# Assignment helpers (SQL Server — unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def get_assigned_agent_ids(user_id: int) -> list:
    """Return the list of hub agent IDs explicitly assigned to a user."""
    rows = _ss_exec(
        "SELECT agent_id FROM hub_agent_assignments WHERE user_id = ?",
        (user_id,), fetchall=True
    ) or []
    return [r['agent_id'] for r in rows]


def get_assigned_workflow_ids(user_id: int) -> list:
    """Return the list of hub workflow IDs explicitly assigned to a user."""
    rows = _ss_exec(
        "SELECT workflow_id FROM hub_workflow_assignments WHERE user_id = ?",
        (user_id,), fetchall=True
    ) or []
    return [r['workflow_id'] for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
# Human-in-the-Loop — approval tool + orchestrator wrappers
# ═════════════════════════════════════════════════════════════════════════════

_APPROVAL_ENABLED = os.getenv('APPROVAL', 'false').lower() == 'true'

_approval_tool_registered = False


def _request_human_approval_fn(title: str = '', context: str = '',
                                assigned_to_user_id=None, **kwargs) -> dict:
    """Built-in hub tool: creates an approval request in SQL Server.

    Registered into TOOL_REGISTRY as ``request_human_approval`` (see
    ``_ensure_approval_tool``) so any agent can call it like a normal tool.
    The agent/orchestrator loop is expected to stop and wait once it sees
    the returned ``status: pending`` message.

    Args:
        title: Short title for the approval request.
        context: Full explanation of what needs approval and why.
        assigned_to_user_id: Specific approver's user ID, or None for any admin.
        **kwargs: Must include ``_hub_ctx`` (injected by HubExecutor) with
            ``user``, ``agent_id``, ``agent_name``, ``convo_id``.

    Returns:
        Dict with ``status: 'pending'``, the new ``approval_id``, and a
        ``message`` instructing the agent to pause until resolved.
    """
    hub_ctx     = kwargs.get('_hub_ctx') or {}
    user        = hub_ctx.get('user') or {}
    approval_id = str(uuid.uuid4())

    ctx_blob = json.dumps({
        'context':         context,
        'agent_name':      hub_ctx.get('agent_name', ''),
        'conversation_id': hub_ctx.get('convo_id', ''),
        'requested_by':    user.get('username', ''),
    })

    _ss_exec("""
        INSERT INTO hub_approvals
            (approval_id, request_type, agent_id, agent_name,
             conversation_id, requested_by_user_id, assigned_to_user_id,
             title, context_json)
        VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, ?)
    """, (approval_id,
          hub_ctx.get('agent_id', ''),
          hub_ctx.get('agent_name', ''),
          hub_ctx.get('convo_id', ''),
          user.get('id'),
          int(assigned_to_user_id) if assigned_to_user_id else None,
          title or 'Approval Required',
          ctx_blob))

    return {
        'status':      'pending',
        'approval_id': approval_id,
        'message': (
            f'Human approval request created (ID: {approval_id}). '
            'Stop your current action and inform the user that approval is pending. '
            'Do not proceed until the approver responds.'
        ),
    }


def _ensure_approval_tool():
    """Lazily register request_human_approval into TOOL_REGISTRY (runs once)."""
    global _approval_tool_registered
    if _approval_tool_registered:
        return
    try:
        from core.tools.registry import TOOL_REGISTRY
        TOOL_REGISTRY['request_human_approval'] = {
            'display_name': 'Request Human Approval',
            'category':     'Human in the Loop',
            'description': (
                'Request human approval before executing a sensitive or irreversible action. '
                'Pauses the agent and sends an approval request to the specified user or any '
                'available admin. Always include full context of what you plan to do and why.'
            ),
            'function': _request_human_approval_fn,
            'schema': {
                'title': {
                    'type': 'string', 'required': True,
                    'description': 'Short title for the approval request (max 200 chars)',
                },
                'context': {
                    'type': 'string', 'required': True,
                    'description': 'Full context: what action needs approval and exactly why',
                },
                'assigned_to_user_id': {
                    'type': 'integer', 'required': False,
                    'description': 'User ID of the specific approver. Omit for any admin.',
                },
            },
            'icon':    '✅',
            'enabled': True,
        }
        _approval_tool_registered = True
        logger.info('[agenthub] request_human_approval tool registered')
    except Exception as exc:
        logger.warning(f'[agenthub] Could not register approval tool: {exc}')


def _ensure_approval_tool_in_ss():
    """Insert request_human_approval into hub_tools (SQL Server) if not present."""
    try:
        existing = _ss_exec(
            "SELECT id FROM hub_tools WHERE name='request_human_approval'", fetchone=True)
        if not existing:
            schema = json.dumps({
                'type': 'object',
                'properties': {
                    'title':   {'type': 'string',  'description': 'Short title for the request'},
                    'context': {'type': 'string',  'description': 'Full context of what needs approval'},
                    'assigned_to_user_id': {'type': 'integer', 'description': 'User ID of approver'},
                },
                'required': ['title', 'context'],
            })
            _ss_exec("""
                INSERT INTO hub_tools
                    (id, name, display_name, description, category, schema_json, enabled)
                VALUES ('hub_builtin_approval', 'request_human_approval',
                        'Request Human Approval',
                        'Pause execution and request a human to approve before proceeding.',
                        'Human in the Loop', ?, 1)
            """, (schema,))
    except Exception as exc:
        logger.warning(f'[agenthub] Could not seed approval tool in hub_tools: {exc}')


_registry_tools_seeded = False  # reset forces re-sync on next request


def _seed_registry_tools_in_ss():
    """Sync every tool from TOOL_REGISTRY into hub_tools (SQL Server).

    Called lazily on the first request that needs the tools list so that
    the Tools page and agent-creation form always show all available tools.
    Skips tools that are already in the table (matched by name), so it is
    safe to call on every request — it exits early after the first run.
    """
    global _registry_tools_seeded
    if _registry_tools_seeded:
        return
    try:
        from core.tools.registry import TOOL_REGISTRY

        for name, tool in TOOL_REGISTRY.items():
            existing = _ss_exec(
                "SELECT id FROM hub_tools WHERE name=?", (name,), fetchone=True)
            if existing:
                continue

            # Convert the flat registry schema to proper JSON Schema
            raw_schema = tool.get("schema", {})
            properties = {}
            required   = []
            for param, meta in raw_schema.items():
                prop = {"type": meta.get("type", "string")}
                if meta.get("description"):
                    prop["description"] = meta["description"]
                if meta.get("enum"):
                    prop["enum"] = meta["enum"]
                properties[param] = prop
                if meta.get("required", False):
                    required.append(param)

            schema_json = json.dumps({
                "type":       "object",
                "properties": properties,
                "required":   required,
            })

            tool_id = f"reg_{name}"
            _ss_exec("""
                INSERT INTO hub_tools
                    (id, name, display_name, description, category, schema_json, enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (tool_id,
                  name,
                  tool.get("display_name", name),
                  tool.get("description", ""),
                  tool.get("category", "General"),
                  schema_json))

        # Remove any hub_tools rows that no longer exist in TOOL_REGISTRY
        # (excludes special built-ins like request_human_approval which aren't in TOOL_REGISTRY)
        registry_names = list(TOOL_REGISTRY.keys())
        if registry_names:
            placeholders = ','.join(['?'] * len(registry_names))
            _ss_exec(
                f"DELETE FROM hub_tools WHERE name NOT IN ({placeholders}) "
                f"AND id LIKE 'reg_%'",
                tuple(registry_names))

        _registry_tools_seeded = True
        logger.info('[agenthub] Registry tools seeded into hub_tools')
    except Exception as exc:
        logger.warning(f'[agenthub] Could not seed registry tools: {exc}')


# ── HubExecutor / HubOrchestrator / HubWorkflowEngine ─────────────────────────
# Relocated to agents/core/hub/ (Phase 3 Slice 1 de-monolith). Imported here as
# plain module-level names so they remain accessible as agents_hub_bp.py module
# attributes — agents/core/tools/registry.py's
# sys.modules['agents_hub_bp'].HubOrchestrator lookup depends on this.
from core.hub.executor import HubExecutor
from core.hub.orchestrator import HubOrchestrator
from core.hub.workflow_engine import HubWorkflowEngine


# ═════════════════════════════════════════════════════════════════════════════
# Permission helpers
# ═════════════════════════════════════════════════════════════════════════════

def _is_dev_or_admin(user):
    """True if user's role is 'admin' or 'dev'."""
    return user and user.get('role') in ('admin', 'dev')


def _is_admin(user):
    """True if user's role is 'admin'."""
    return user and user.get('role') == 'admin'


def _can_access_agent(user, agent_id: str) -> bool:
    """Check whether a user may access a given hub agent.

    Admins can access all agents. Devs can additionally access agents they
    created. All other access is governed by explicit assignment.

    Args:
        user: Current user dict (role, id).
        agent_id: Agent ID to check.

    Returns:
        True if the user is admin, created the agent (dev only), or has it
        explicitly assigned.
    """
    if _is_admin(user):
        return True
    if user.get('role') == 'dev':
        # Devs can access agents they created
        row = _ss_exec('SELECT created_by FROM hub_agents WHERE id=?', (agent_id,), fetchone=True)
        if row and row.get('created_by') == user['id']:
            return True
    ids = get_assigned_agent_ids(user['id'])
    return agent_id in ids


def _can_access_workflow(user, workflow_id: str) -> bool:
    """True if the user is admin or has this workflow explicitly assigned."""
    if _is_admin(user):
        return True
    ids = get_assigned_workflow_ids(user['id'])
    return workflow_id in ids


def _tool_names(tools):
    """Extract plain tool name strings from either string or {name, config} objects."""
    return [t if isinstance(t, str) else t.get('name', '') for t in (tools or [])]


def _extract_tool_configs(tools):
    """Return {tool_name: config_dict} from a tools list that may contain {name, config} objects."""
    configs = {}
    for t in (tools or []):
        if isinstance(t, dict) and t.get('name') and t.get('config'):
            configs[t['name']] = t['config']
    return configs


def _agent_dict(row):
    """Convert a raw hub_agents row into an API-ready dict.

    Normalizes the ``tools`` column into a list of ``{name, config}`` dicts
    (accepting both legacy plain-string entries and the newer config-object
    form) and parses ``env_vars_json``.

    Args:
        row: Raw DB row (dict-like) from hub_agents, or None.

    Returns:
        The normalized agent dict, or None if ``row`` is None.
    """
    if not row:
        return None
    d = _fix_row(dict(row))
    tools_raw = json.loads(d.pop('tools_json', '[]'))
    tools = []
    for t in tools_raw:
        if isinstance(t, str):
            tools.append({'name': t, 'config': {}})
        elif isinstance(t, dict) and t.get('name'):
            tools.append({'name': t['name'], 'config': t.get('config', {})})
    d['tools'] = tools
    d['env_vars'] = json.loads(d.pop('env_vars_json', '{}') or '{}')
    return d


AGENT_COLORS = ['#6366f1', '#ec4899', '#14b8a6', '#f59e0b',
                '#10b981', '#ef4444', '#8b5cf6', '#06b6d4']


def _enrich_org(agents: list) -> list:
    """Add dept_ids/project_ids arrays (and legacy single-value fields) to agent dicts."""
    try:
        import org_db
        ids      = [a.get('id') for a in agents if a.get('id')]
        org_map  = org_db.get_resource_orgs_batch('hub_agent', ids)
        depts    = {d['id']: d for d in org_db.get_all_departments()}
        projects = {p['id']: p for p in org_db.get_all_projects()}
        for a in agents:
            orgs     = org_map.get(str(a.get('id')), {})
            dept_ids = orgs.get('dept_ids', [])
            proj_ids = orgs.get('project_ids', [])
            a['dept_ids']      = dept_ids
            a['project_ids']   = proj_ids
            a['dept_names']    = [depts[d]['name']  for d in dept_ids if d in depts]
            a['dept_colors']   = [depts[d]['color'] for d in dept_ids if d in depts]
            a['project_names'] = [projects[p]['name'] for p in proj_ids if p in projects]
            # Legacy single-value fields kept for backward compat
            a['department_id'] = dept_ids[0] if dept_ids else None
            a['dept_name']     = a['dept_names'][0]    if a['dept_names']    else None
            a['dept_color']    = a['dept_colors'][0]   if a['dept_colors']   else None
            a['project_id']    = proj_ids[0] if proj_ids else None
            a['project_name']  = a['project_names'][0] if a['project_names'] else None
    except Exception:
        pass
    return agents


_TOOL_ENVS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Data', 'tool_envs')


def _get_venv_python(tool_name: str):
    """Return path to the venv Python for this tool, or None if the venv hasn't been created."""
    venv_dir = os.path.join(_TOOL_ENVS_DIR, tool_name)
    py = (os.path.join(venv_dir, 'Scripts', 'python.exe') if sys.platform == 'win32'
          else os.path.join(venv_dir, 'bin', 'python'))
    return py if os.path.isfile(py) else None


def _venv_dir(tool_name: str) -> str:
    """Return the directory path for a custom tool's dedicated venv."""
    return os.path.join(_TOOL_ENVS_DIR, tool_name)


# ═════════════════════════════════════════════════════════════════════════════
# API — CUSTOM TOOLS
# ═════════════════════════════════════════════════════════════════════════════

_custom_tools_loaded = False


def _load_custom_tools_into_registry():
    """Load all enabled custom tools from hub_custom_tools into TOOL_REGISTRY."""
    global _custom_tools_loaded
    if _custom_tools_loaded:
        return
    try:
        from core.tools.registry import register_custom_tool
        rows = _ss_exec(
            "SELECT * FROM hub_custom_tools WHERE enabled=1 ORDER BY created_at",
            fetchall=True) or []
        for row in rows:
            try:
                register_custom_tool(_fix_row(row))
            except Exception as e:
                logger.warning(f"[custom-tools] Failed to register '{row.get('name')}': {e}")
        _custom_tools_loaded = True
        logger.info(f"[custom-tools] Loaded {len(rows)} custom tools into TOOL_REGISTRY")
    except Exception as exc:
        logger.warning(f"[custom-tools] Registry load error: {exc}")


def _exec_custom_tool(tool_row: dict, params: dict, hub_ctx: dict = None) -> dict:
    """Execute a custom tool.

    Uses the tool's dedicated venv (subprocess) when available, falls back to
    exec() in the main process otherwise.  Agent env vars override tool-level
    defaults and are injected into the subprocess environment / os.environ.
    """
    tool_name     = tool_row.get("name", "")
    pip_packages  = json.loads(tool_row.get("pip_packages") or "[]")
    imports_code  = tool_row.get("imports_code") or ""
    function_code = tool_row.get("function_code") or "def run(**kwargs):\n    return {}"
    tool_env_vars = json.loads(tool_row.get("env_vars_json") or "{}")

    # Merge: tool defaults < agent overrides
    agent_env_vars  = (hub_ctx or {}).get("agent_env_vars", {})
    merged_env_vars = {**tool_env_vars, **agent_env_vars}

    venv_py = _get_venv_python(tool_name)
    if venv_py:
        return _exec_via_venv(venv_py, imports_code, function_code, params, merged_env_vars)
    return _exec_inline(pip_packages, imports_code, function_code, params, merged_env_vars)


def _exec_via_venv(venv_py: str, imports_code: str, function_code: str,
                   params: dict, env_vars: dict) -> dict:
    """Write a temp .py file and run it under the tool's venv Python."""
    import subprocess, tempfile

    env_inject = repr(env_vars)   # safe literal for embedding in script
    params_literal = json.dumps(params)

    script = f"""import json, os, sys

_env = {env_inject}
for _k, _v in _env.items():
    os.environ[str(_k)] = str(_v)

{imports_code}

{function_code}

_params = json.loads({json.dumps(params_literal)!r})
try:
    _result = run(**_params)
    print(json.dumps({{"success": True, "result": _result}}))
except Exception as _e:
    print(json.dumps({{"success": False, "error": str(_e)}}))
"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='_tool_run.py',
                                          delete=False, encoding='utf-8') as f:
            f.write(script)
            tmp = f.name

        proc = subprocess.run([venv_py, tmp], capture_output=True, text=True, timeout=60)
        stdout = (proc.stdout or "").strip()
        if not stdout:
            return {"success": False,
                    "error": proc.stderr.strip() or "No output from tool subprocess"}
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Tool execution timed out (60s)"}
    except json.JSONDecodeError as e:
        raw = (proc.stdout or "")[:500] if 'proc' in dir() else ""
        return {"success": False, "error": f"Invalid JSON output from tool: {e}", "raw": raw}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _exec_inline(pip_packages: list, imports_code: str, function_code: str,
                  params: dict, env_vars: dict) -> dict:
    """Fallback executor — exec() in the main process with temp env var injection."""
    import subprocess, importlib

    # Temporarily inject env vars
    old_env = {}
    for k, v in env_vars.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = str(v)

    try:
        install_errors = []
        for pkg in pip_packages:
            pkg_name = pkg.split("==")[0].split(">=")[0].strip()
            try:
                importlib.import_module(pkg_name.replace("-", "_"))
            except ImportError:
                try:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", pkg],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                except Exception as e:
                    install_errors.append(f"{pkg}: {e}")

        if install_errors:
            return {"success": False,
                    "error": "Package install failed: " + "; ".join(install_errors)}

        ns = {}
        try:
            if imports_code.strip():
                exec(compile(imports_code, "<imports>", "exec"), ns)
            exec(compile(function_code, "<function_code>", "exec"), ns)
        except SyntaxError as e:
            return {"success": False, "error": f"Syntax error: {e}"}

        run_fn = ns.get("run")
        if not callable(run_fn):
            return {"success": False, "error": "function_code must define a callable named 'run'"}

        result = run_fn(**params)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        for k, old_v in old_env.items():
            if old_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_v


def _sync_custom_tool_to_hub_tools(tool_row: dict):
    """Upsert a custom tool into hub_tools so it shows in the agent builder."""
    name         = tool_row["name"]
    input_schema = json.loads(tool_row.get("input_schema") or "[]")
    properties   = {}
    required     = []
    for p in input_schema:
        properties[p["name"]] = {
            "type":        p.get("type", "string"),
            "description": p.get("description", ""),
        }
        if p.get("required"):
            required.append(p["name"])
    schema_json = json.dumps({"type": "object", "properties": properties, "required": required})
    tool_id = f"custom_{name}"
    existing = _ss_exec("SELECT id FROM hub_tools WHERE id=?", (tool_id,), fetchone=True)
    if existing:
        _ss_exec("""
            UPDATE hub_tools SET
                name=?, display_name=?, description=?, category=?,
                schema_json=?, enabled=?
            WHERE id=?
        """, (name, tool_row.get("display_name", name), tool_row.get("description", ""),
              tool_row.get("category", "Custom"), schema_json,
              1 if tool_row.get("enabled", True) else 0, tool_id))
    else:
        _ss_exec("""
            INSERT INTO hub_tools (id, name, display_name, description, category, schema_json, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tool_id, name, tool_row.get("display_name", name),
              tool_row.get("description", ""), tool_row.get("category", "Custom"),
              schema_json, 1 if tool_row.get("enabled", True) else 0))


def _custom_tool_dict(row):
    """Convert a raw hub_custom_tools row into an API-ready dict (parses JSON columns)."""
    if not row:
        return None
    d = _fix_row(dict(row))
    d["pip_packages"] = json.loads(d.get("pip_packages") or "[]")
    d["input_schema"] = json.loads(d.get("input_schema") or "[]")
    d["env_vars"]     = json.loads(d.pop("env_vars_json", "{}") or "{}")
    d["venv_path"]    = d.get("venv_path")
    return d


# ── Route registration (Phase 3 Slice 4 — moved out of agents_hub_bp.py) ────
# Each import below is a pure side-effect import: importing the module runs
# its @agents_hub_bp.route(...) decorators, which register those routes onto
# THIS SAME Blueprint object (no new blueprint names, no url_for() changes —
# see the Phase 3 Slice 4 plan for why this is safe for a real Flask
# Blueprint, unlike app.py's Slice 2 situation).
import agents_hub_pages_routes
import agents_hub_agents_routes
import agents_hub_chat_routes
import agents_hub_document_routes
import agents_hub_workflows_routes
import agents_hub_admin_routes
import agents_hub_analytics_routes
import agents_hub_approvals_routes
import agents_hub_custom_tools_routes
import agents_hub_tool_jobs_routes

