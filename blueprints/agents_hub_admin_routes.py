"""Smaller Hub Agents admin/config routes for agents_hub_bp: tools
registry enable/disable/test, legacy hub knowledge base, generic
(non-tool) scheduled jobs, chat guardrails, and misc read-only lookups
(connections/users/my-access). Split out of agents_hub_bp.py in Phase 3
Slice 4 — grouped together as small, related admin-surface endpoints
rather than one file per 3-4-route concern.
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
    _can_access_agent, _can_access_workflow,
    get_assigned_agent_ids, get_assigned_workflow_ids,
    _APPROVAL_ENABLED, _ensure_approval_tool, _org, logger,
    _ensure_approval_tool_in_ss, _seed_registry_tools_in_ss,
)


@agents_hub_bp.route('/api/agenthub/tools', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_tools():
    if _APPROVAL_ENABLED:
        _ensure_approval_tool()
        _ensure_approval_tool_in_ss()
    _seed_registry_tools_in_ss()
    rows = _ss_exec('SELECT * FROM hub_tools ORDER BY category, name', fetchall=True) or []
    return jsonify([{**_fix_row(r), 'schema': json.loads(r.get('schema_json') or '{}')}
                    for r in rows])


@agents_hub_bp.route('/api/agenthub/tools/<tid>/toggle', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_toggle_tool(tid):
    row = _ss_exec('SELECT enabled FROM hub_tools WHERE id=?', (tid,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    _ss_exec('UPDATE hub_tools SET enabled=? WHERE id=?',
             (0 if row['enabled'] else 1, tid))
    row = _ss_exec('SELECT * FROM hub_tools WHERE id=?', (tid,), fetchone=True)
    return jsonify({**_fix_row(row), 'schema': json.loads(row.get('schema_json') or '{}')})


@agents_hub_bp.route('/api/agenthub/tools/test', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_test_tool():
    data      = request.json or {}
    tool_name = data.get('tool')
    params    = data.get('params', {})
    from core.tools.registry import execute_tool
    result  = execute_tool(tool_name, params)
    success = 1 if result.get('success') else 0
    _ss_exec("""
        UPDATE hub_tools
        SET total_calls=total_calls+1, success_calls=success_calls+?
        WHERE name=?
    """, (success, tool_name))
    return jsonify(result)


@agents_hub_bp.route('/api/agenthub/knowledge', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_knowledge():
    rows = _ss_exec(
        'SELECT * FROM hub_knowledge_bases ORDER BY created_at DESC', fetchall=True) or []
    return jsonify(_fix_rows(rows))


@agents_hub_bp.route('/api/agenthub/knowledge', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_upload_knowledge():
    d       = request.json or {}
    content = d.get('content', '')
    kid     = str(uuid.uuid4())
    _ss_exec(
        'INSERT INTO hub_knowledge_bases (id,name,description,file_type,content,file_size,chunk_count) '
        'VALUES (?,?,?,?,?,?,?)',
        (kid, d.get('name', 'Document'), d.get('description', ''), d.get('file_type', 'txt'),
         content, len(content), max(1, len(content) // 500)))
    row = _ss_exec('SELECT * FROM hub_knowledge_bases WHERE id=?', (kid,), fetchone=True)
    return jsonify(_fix_row(row)), 201


@agents_hub_bp.route('/api/agenthub/knowledge/<kid>', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def hub_delete_knowledge(kid):
    _ss_exec('DELETE FROM hub_knowledge_bases WHERE id=?', (kid,))
    return jsonify({'success': True})


@agents_hub_bp.route('/api/agenthub/jobs', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_jobs():
    rows = _ss_exec(
        "SELECT * FROM hub_jobs WHERE job_type != 'tool' ORDER BY created_at DESC",
        fetchall=True) or []
    return jsonify(_fix_rows(rows))


@agents_hub_bp.route('/api/agenthub/jobs', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_create_job():
    d   = request.json or {}
    jid = str(uuid.uuid4())
    _ss_exec(
        'INSERT INTO hub_jobs (id,name,description,job_type,target_id,schedule,department_id,project_id) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (jid, d.get('name', 'New Job'), d.get('description', ''),
         d.get('job_type', 'agent'), d.get('target_id', ''),
         d.get('schedule', '0 9 * * *'),
         d.get('department_id'), d.get('project_id')))
    row = _ss_exec('SELECT * FROM hub_jobs WHERE id=?', (jid,), fetchone=True)
    return jsonify(_fix_row(row)), 201


@agents_hub_bp.route('/api/agenthub/jobs/<jid>/toggle', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_toggle_job(jid):
    row = _ss_exec('SELECT status FROM hub_jobs WHERE id=?', (jid,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    new_status = 'paused' if row['status'] == 'active' else 'active'
    _ss_exec('UPDATE hub_jobs SET status=? WHERE id=?', (new_status, jid))
    row = _ss_exec('SELECT * FROM hub_jobs WHERE id=?', (jid,), fetchone=True)
    return jsonify(_fix_row(row))


@agents_hub_bp.route('/api/agenthub/jobs/<jid>', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def hub_delete_job(jid):
    _ss_exec('DELETE FROM hub_jobs WHERE id=?', (jid,))
    return jsonify({'success': True})


@agents_hub_bp.route('/api/agenthub/admin/guardrails', methods=['GET'])
@auth.login_required
@auth.admin_required
def hub_get_guardrail():
    """GET ?scope_type=user|department|project&scope_id=X&agent_id=Y&agent_type=hub → single guardrail config.
       Legacy: ?user_id=X&agent_id=Y&agent_type=hub → single per-user guardrail config (scope_type=user).
       GET (no params) → all guardrails (any scope) for admin list."""
    _odb       = _org()
    scope_type = request.args.get('scope_type', default=None)
    scope_id   = request.args.get('scope_id',   type=int)
    user_id    = request.args.get('user_id',    type=int)
    agent_id   = request.args.get('agent_id',   default=None)
    agent_type = request.args.get('agent_type', default='hub')

    if scope_type is None and user_id is not None:
        scope_type, scope_id = 'user', user_id

    if scope_type and scope_id is not None and agent_id:
        g = _odb.get_scoped_guardrail(scope_type, scope_id, agent_id, agent_type)
        return jsonify({'guardrail': g or {}})
    return jsonify({'guardrails': _odb.get_all_guardrails()})


@agents_hub_bp.route('/api/agenthub/admin/guardrails', methods=['PUT'])
@auth.login_required
@auth.admin_required
def hub_save_guardrail():
    admin = auth.current_user()
    data  = request.json or {}
    try:
        scope_type = data.get('scope_type') or 'user'
        scope_id   = data.get('scope_id')
        if scope_id is None:
            scope_id = data['user_id']  # legacy payloads
        ok, msg = _org().set_scoped_guardrail(
            scope_type         = str(scope_type),
            scope_id           = int(scope_id),
            agent_id           = str(data['agent_id']),
            agent_type         = data.get('agent_type', 'hub'),
            filter_rules       = data.get('filter_rules', []),
            restrict_tables    = data.get('restrict_tables') or None,
            custom_instruction = data.get('custom_instruction', ''),
            created_by         = admin['id'],
        )
        return jsonify({'success': ok, 'message': msg}), (200 if ok else 500)
    except (KeyError, ValueError) as e:
        return jsonify({'error': str(e)}), 400


@agents_hub_bp.route('/api/agenthub/admin/guardrails', methods=['DELETE'])
@auth.login_required
@auth.admin_required
def hub_delete_guardrail():
    data       = request.json or {}
    scope_type = data.get('scope_type') or 'user'
    scope_id   = data.get('scope_id')
    if scope_id is None:
        scope_id = data['user_id']  # legacy payloads
    ok, msg = _org().delete_scoped_guardrail(
        str(scope_type), int(scope_id), str(data['agent_id']),
        data.get('agent_type', 'hub'),
    )
    return jsonify({'success': ok, 'message': msg})


@agents_hub_bp.route('/api/agenthub/connections', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_db_connections():
    """Return available database connections (no passwords) for workflow editor."""
    try:
        from database_manager import DatabaseConnectionManager
        mgr = DatabaseConnectionManager()
        conns = mgr.load_connections() or []
        safe = [{'name': c.get('name', ''), 'type': c.get('type', ''),
                 'database': c.get('database', ''), 'server': c.get('server', '')}
                for c in conns]
        return jsonify(safe)
    except Exception as exc:
        logger.warning(f'[agenthub] Could not load connections: {exc}')
        return jsonify([])


@agents_hub_bp.route('/api/agenthub/users', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_users_for_approver():
    rows = _ss_exec(
        "SELECT u.id, u.username, r.name AS role "
        "FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE u.is_active=1 ORDER BY u.username",
        fetchall=True) or []
    return jsonify(rows)


@agents_hub_bp.route('/api/agenthub/my-access', methods=['GET'])
@auth.login_required
def hub_my_access():
    user = auth.current_user()
    if _is_admin(user):
        return jsonify({'role': user['role'], 'all_access': True,
                        'agent_ids': [], 'workflow_ids': []})
    return jsonify({
        'role':         user['role'],
        'all_access':   False,
        'agent_ids':    get_assigned_agent_ids(user['id']),
        'workflow_ids': get_assigned_workflow_ids(user['id']),
    })
