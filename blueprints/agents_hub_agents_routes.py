"""Org-departments lookup + Hub Agents CRUD routes for agents_hub_bp.
Split out of agents_hub_bp.py in Phase 3 Slice 4.
"""

import sys, os, uuid, json, logging, threading, re, calendar
from datetime import datetime, timedelta, date
from flask import (render_template, jsonify, request,
                   Response, stream_with_context, redirect, url_for, abort)
import auth
import token_limits
from agents_hub_bp import agents_hub_bp
from agents_hub_bp import (
    _ss_exec, _fix_row, _fix_rows, _is_dev_or_admin, _is_admin,
    _can_access_agent, get_assigned_agent_ids,
    _agent_dict, AGENT_COLORS, _enrich_org, _org,
)


@agents_hub_bp.route('/api/agenthub/org/departments', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_departments():
    try:
        depts = _org().get_all_departments()
    except Exception:
        depts = []
    return jsonify(depts)


@agents_hub_bp.route('/api/agenthub/agents', methods=['GET'])
@auth.login_required
def hub_list_agents():
    user = auth.current_user()
    if _is_admin(user):
        agents = _ss_exec(
            'SELECT * FROM hub_agents WHERE status=? ORDER BY created_at DESC',
            ('active',), fetchall=True) or []
        agents = [_agent_dict(a) for a in agents]
    elif user.get('role') == 'dev':
        # Devs see active agents they created OR that were assigned to them
        agents = _ss_exec(
            'SELECT * FROM hub_agents WHERE status=? ORDER BY created_at DESC',
            ('active',), fetchall=True) or []
        agents = [_agent_dict(a) for a in agents]
        assigned = set(get_assigned_agent_ids(user['id']))
        agents = [a for a in agents if a.get('created_by') == user['id'] or a['id'] in assigned]
    else:
        agents = _ss_exec(
            'SELECT * FROM hub_agents WHERE status=? ORDER BY created_at DESC',
            ('active',), fetchall=True) or []
        agents = [_agent_dict(a) for a in agents]
        assigned = set(get_assigned_agent_ids(user['id']))
        agents = [a for a in agents if a['id'] in assigned]
    return jsonify(_enrich_org(agents))


@agents_hub_bp.route('/api/agenthub/agents/all', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_all_agents():
    user = auth.current_user()
    if _is_admin(user):
        rows = _ss_exec('SELECT * FROM hub_agents ORDER BY created_at DESC', fetchall=True) or []
    else:
        # Dev: agents they created + agents assigned to them (all statuses)
        rows = _ss_exec('SELECT * FROM hub_agents ORDER BY created_at DESC', fetchall=True) or []
        assigned = set(get_assigned_agent_ids(user['id']))
        rows = [r for r in rows
                if (r.get('created_by') == user['id'] or r.get('id') in assigned)]
    return jsonify(_enrich_org([_agent_dict(r) for r in rows]))


@agents_hub_bp.route('/api/agenthub/agents/<aid>', methods=['GET'])
@auth.login_required
def hub_get_agent(aid):
    user = auth.current_user()
    if not _can_access_agent(user, aid):
        return jsonify({'error': 'Not assigned'}), 403
    row = _ss_exec('SELECT * FROM hub_agents WHERE id=?', (aid,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_agent_dict(row))


@agents_hub_bp.route('/api/agenthub/agents', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_create_agent():
    d          = request.json or {}
    creator    = auth.current_user()
    count      = (_ss_exec('SELECT COUNT(*) AS c FROM hub_agents', fetchone=True) or {}).get('c', 0)
    aid        = str(uuid.uuid4())
    created_by = creator.get('id', 0) if creator else 0
    # Resolve dept/project IDs — accept arrays (new) or single value (backward compat)
    dept_ids = d.get('dept_ids') or ([d['department_id']] if d.get('department_id') else [])
    proj_ids = d.get('project_ids') or ([d['project_id']] if d.get('project_id') else [])
    primary_dept = dept_ids[0] if dept_ids else None
    primary_proj = proj_ids[0] if proj_ids else None
    _ss_exec(
        'INSERT INTO hub_agents (id,name,description,objective,system_prompt,model,provider,temperature,'
        'tools_json,avatar_color,env_vars_json,department_id,project_id,created_by) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (aid, d.get('name', 'New Agent'), d.get('description', ''), d.get('objective', ''),
         d.get('system_prompt', ''), d.get('model', 'gpt-4o'), d.get('provider', 'openai'),
         d.get('temperature', 0.7), json.dumps(d.get('tools', [])),
         d.get('avatar_color', AGENT_COLORS[count % len(AGENT_COLORS)]),
         json.dumps(d.get('env_vars', {})),
         primary_dept, primary_proj, created_by))
    try:
        import org_db
        org_db.set_resource_org('hub_agents', aid, dept_ids, proj_ids)
    except Exception:
        pass
    row = _ss_exec('SELECT * FROM hub_agents WHERE id=?', (aid,), fetchone=True)
    return jsonify(_enrich_org([_agent_dict(row)])[0]), 201


@agents_hub_bp.route('/api/agenthub/agents/<aid>', methods=['PUT'])
@auth.login_required
@auth.dev_or_admin_required
def hub_update_agent(aid):
    d = request.json or {}
    fields, vals = [], []
    for f in ['name', 'description', 'objective', 'system_prompt', 'model', 'provider',
              'temperature', 'avatar_color', 'status']:
        if f in d:
            fields.append(f'{f}=?')
            vals.append(d[f])
    if 'tools' in d:
        fields.append('tools_json=?')
        vals.append(json.dumps(d['tools']))
    if 'env_vars' in d:
        fields.append('env_vars_json=?')
        vals.append(json.dumps(d['env_vars']))
    # Handle org: accept arrays (new) or single values (backward compat)
    if 'dept_ids' in d or 'project_ids' in d:
        dept_ids = d.get('dept_ids') or []
        proj_ids = d.get('project_ids') or []
        try:
            import org_db
            org_db.set_resource_org('hub_agents', aid, dept_ids, proj_ids)
        except Exception:
            pass
        # Sync primary value to legacy column
        fields.append('department_id=?'); vals.append(dept_ids[0] if dept_ids else None)
        fields.append('project_id=?');    vals.append(proj_ids[0] if proj_ids else None)
    elif 'department_id' in d or 'project_id' in d:
        dept_id = d.get('department_id')
        proj_id = d.get('project_id')
        dept_ids = [dept_id] if dept_id else []
        proj_ids = [proj_id] if proj_id else []
        try:
            import org_db
            org_db.set_resource_org('hub_agents', aid, dept_ids, proj_ids)
        except Exception:
            pass
        fields.append('department_id=?'); vals.append(dept_id)
        fields.append('project_id=?');    vals.append(proj_id)
    if fields:
        vals += [datetime.utcnow().isoformat(), aid]
        _ss_exec(f'UPDATE hub_agents SET {",".join(fields)},updated_at=? WHERE id=?', tuple(vals))
    row = _ss_exec('SELECT * FROM hub_agents WHERE id=?', (aid,), fetchone=True)
    return jsonify(_enrich_org([_agent_dict(row)])[0])


@agents_hub_bp.route('/api/agenthub/agents/<aid>', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def hub_delete_agent(aid):
    # Cascade: remove org assignments and guardrails so no orphan rows accumulate
    _ss_exec("DELETE FROM resource_departments WHERE resource_type='hub_agent' AND resource_id=?", (aid,))
    _ss_exec("DELETE FROM resource_projects   WHERE resource_type='hub_agent' AND resource_id=?", (aid,))
    _ss_exec("DELETE FROM agent_guardrails     WHERE agent_id=? AND agent_type='hub'", (aid,))
    _ss_exec('DELETE FROM hub_agents WHERE id=?', (aid,))
    return jsonify({'success': True})


@agents_hub_bp.route('/api/agenthub/agents/<aid>/add-doc', methods=['POST'])
@auth.login_required
def hub_agent_add_doc(aid):
    """
    Append a document_id to this agent's search_knowledge tool config.
    Called automatically when a user uploads a file to the KB from the chat.
    Any user who can chat with the agent can add docs from chat.
    """
    user = auth.current_user()
    if not _can_access_agent(user, aid):
        return jsonify({'error': 'Access denied'}), 403

    data    = request.get_json() or {}
    doc_id  = (data.get('document_id') or '').strip()
    if not doc_id:
        return jsonify({'error': 'document_id required'}), 400

    row = _ss_exec('SELECT tools_json FROM hub_agents WHERE id=?', (aid,), fetchone=True)
    if not row:
        return jsonify({'error': 'Agent not found'}), 404

    tools = json.loads(row.get('tools_json') or '[]')

    # Find the existing search_knowledge tool entry, if any
    tool_existed = False
    sk_entry = None
    for t in tools:
        if isinstance(t, dict) and t.get('name') == 'search_knowledge':
            sk_entry = t
            tool_existed = True
            break
        if isinstance(t, str) and t == 'search_knowledge':
            # Convert plain string to config object
            tools.remove(t)
            sk_entry = {'name': 'search_knowledge', 'config': {}}
            tools.append(sk_entry)
            tool_existed = True
            break

    if sk_entry is None:
        # Tool not yet in agent — add it (starts curated to just this upload,
        # same as picking specific documents in the agent builder)
        sk_entry = {'name': 'search_knowledge', 'config': {}}
        tools.append(sk_entry)

    if not isinstance(sk_entry.get('config'), dict):
        sk_entry['config'] = {}

    existing = sk_entry['config'].get('document_ids', [])
    if isinstance(existing, str):
        existing = [e.strip() for e in existing.split(',') if e.strip()]

    if tool_existed and not existing:
        # The tool already existed and had no document restriction — i.e. it
        # was unrestricted ("search everything visible"). Leave it that way:
        # the new document is already covered by the open search, and adding
        # it here would narrow the agent down to ONLY chat-uploaded documents.
        document_ids = existing
    else:
        # Brand-new tool, or an already-curated list — extend it.
        if doc_id not in existing:
            existing.append(doc_id)
        document_ids = existing
        sk_entry['config']['document_ids'] = document_ids

    _ss_exec(
        'UPDATE hub_agents SET tools_json=?, updated_at=? WHERE id=?',
        (json.dumps(tools), datetime.utcnow().isoformat(), aid))

    return jsonify({'success': True, 'document_id': doc_id, 'document_ids': document_ids})


def _agent_sharepoint_watch_ids(tools_json: str) -> list:
    """Extract the distinct SharePoint watch-config IDs referenced in an agent's
    connector-aware tool configs (search_connector_knowledge / list_connector_documents)."""
    tools = json.loads(tools_json or '[]')
    ids = set()
    for t in tools:
        if not isinstance(t, dict) or t.get('name') not in (
                'search_connector_knowledge', 'list_connector_documents'):
            continue
        for key in (t.get('config') or {}).get('connector_keys') or []:
            if isinstance(key, str) and key.startswith('sharepoint:'):
                suffix = key.split(':', 1)[1]
                if suffix.isdigit():
                    ids.add(int(suffix))
    return sorted(ids)


@agents_hub_bp.route('/api/agenthub/agents/<aid>/sharepoint-connectors', methods=['GET'])
@auth.login_required
def hub_agent_sharepoint_connectors(aid):
    """
    List the SharePoint connectors (id, label) attached to this agent.

    Any user who can chat with the agent may call this (unlike the
    admin/dev-only /api/knowledge/sharepoint-watches), so the chat UI can
    offer a "which SharePoint site" picker when adding a document.
    """
    user = auth.current_user()
    if not _can_access_agent(user, aid):
        return jsonify({'error': 'Access denied'}), 403

    row = _ss_exec('SELECT tools_json FROM hub_agents WHERE id=?', (aid,), fetchone=True)
    if not row:
        return jsonify({'error': 'Agent not found'}), 404

    watch_ids = _agent_sharepoint_watch_ids(row.get('tools_json'))
    if not watch_ids:
        return jsonify({'success': True, 'connectors': []})

    placeholders = ','.join('?' * len(watch_ids))
    rows = _ss_exec(
        f'SELECT id, label FROM sharepoint_watch_configs WHERE enabled=1 AND id IN ({placeholders})',
        tuple(watch_ids), fetchall=True) or []
    connectors = [{'id': r['id'], 'label': r.get('label') or f"SharePoint #{r['id']}"} for r in rows]
    return jsonify({'success': True, 'connectors': connectors})
