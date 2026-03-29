"""Hub Agents workflow CRUD/run/resume routes for agents_hub_bp.
hub_resume_workflow is relocated here from its original physical location
in the file's approvals section (it resumes a workflow paused on an
approval node, so it is a workflows concern by URL and purpose — same
behavior, verbatim body, different file). Split out in Phase 3 Slice 4.
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
    _can_access_workflow, get_assigned_workflow_ids,
    _tool_names, _extract_tool_configs,
    _APPROVAL_ENABLED, _ensure_approval_tool,
    HubExecutor, HubOrchestrator, HubWorkflowEngine,
)


def _wf_dict(row):
    """Convert a raw hub_workflows row into an API-ready dict (parses nodes/edges JSON)."""
    if not row:
        return None
    d = _fix_row(dict(row))
    d['nodes'] = json.loads(d.pop('nodes_json', '[]'))
    d['edges'] = json.loads(d.pop('edges_json', '[]'))
    return d


@agents_hub_bp.route('/api/agenthub/workflows', methods=['GET'])
@auth.login_required
def hub_list_workflows():
    user = auth.current_user()
    rows = _ss_exec('SELECT * FROM hub_workflows ORDER BY created_at DESC', fetchall=True) or []
    wfs  = [_wf_dict(r) for r in rows]
    if not _is_admin(user):
        assigned = set(get_assigned_workflow_ids(user['id']))
        wfs = [w for w in wfs if w['id'] in assigned]
    return jsonify(wfs)


@agents_hub_bp.route('/api/agenthub/workflows/<wid>', methods=['GET'])
@auth.login_required
def hub_get_workflow(wid):
    user = auth.current_user()
    if not _can_access_workflow(user, wid):
        return jsonify({'error': 'Not assigned'}), 403
    row = _ss_exec('SELECT * FROM hub_workflows WHERE id=?', (wid,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_wf_dict(row))


@agents_hub_bp.route('/api/agenthub/workflows', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def hub_create_workflow():
    d   = request.json or {}
    wid = str(uuid.uuid4())
    _ss_exec(
        'INSERT INTO hub_workflows (id,name,description,nodes_json,edges_json,execution_mode,department_id,project_id) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (wid, d.get('name', 'New Workflow'), d.get('description', ''),
         json.dumps(d.get('nodes', [])), json.dumps(d.get('edges', [])),
         d.get('execution_mode', 'sequential'),
         d.get('department_id'), d.get('project_id')))
    row = _ss_exec('SELECT * FROM hub_workflows WHERE id=?', (wid,), fetchone=True)
    return jsonify(_wf_dict(row)), 201


@agents_hub_bp.route('/api/agenthub/workflows/<wid>', methods=['PUT'])
@auth.login_required
@auth.dev_or_admin_required
def hub_update_workflow(wid):
    d = request.json or {}
    fields, vals = [], []
    for f in ['name', 'description', 'execution_mode', 'status', 'department_id', 'project_id']:
        if f in d:
            fields.append(f'{f}=?')
            vals.append(d[f])
    if 'nodes' in d:
        fields.append('nodes_json=?')
        vals.append(json.dumps(d['nodes']))
    if 'edges' in d:
        fields.append('edges_json=?')
        vals.append(json.dumps(d['edges']))
    if fields:
        vals += [datetime.utcnow().isoformat(), wid]
        _ss_exec(f'UPDATE hub_workflows SET {",".join(fields)},updated_at=? WHERE id=?', tuple(vals))
    row = _ss_exec('SELECT * FROM hub_workflows WHERE id=?', (wid,), fetchone=True)
    return jsonify(_wf_dict(row))


@agents_hub_bp.route('/api/agenthub/workflows/<wid>', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def hub_delete_workflow(wid):
    _ss_exec('DELETE FROM hub_workflows WHERE id=?', (wid,))
    return jsonify({'success': True})


@agents_hub_bp.route('/api/agenthub/workflows/<wid>/run', methods=['POST'])
@auth.login_required
def hub_run_workflow(wid):
    """Run a workflow from the start, streaming NDJSON (POST /api/agenthub/workflows/<wid>/run).

    Enforces the token quota and workflow-access check, creates a
    ``hub_workflow_runs`` record, then drives ``HubWorkflowEngine.execute``.
    If the run pauses at an approval node, the run status is recorded as
    ``waiting_approval`` and token usage is NOT recorded yet (it will be on
    resume); otherwise status becomes ``completed`` and usage is recorded.

    Request JSON: ``input`` (trigger input value; defaults to "Start workflow").

    Returns:
        A streaming ``Response`` of NDJSON lines from
        ``HubWorkflowEngine.execute``, prefixed with a ``run_id`` event.
    """
    user = auth.current_user()
    if not _can_access_workflow(user, wid):
        return jsonify({'error': 'Workflow not assigned to you'}), 403

    allowed, msg = token_limits.check_and_record(user, question='workflow run', agent_name='hub_workflow')
    if not allowed:
        return jsonify({'error': msg}), 429

    wf_row = _ss_exec('SELECT * FROM hub_workflows WHERE id=?', (wid,), fetchone=True)
    if not wf_row:
        return jsonify({'error': 'Not found'}), 404
    wf = _wf_dict(wf_row)

    data       = request.json or {}
    input_data = data.get('input', 'Start workflow')
    api_key    = os.environ.get('OPENAI_API_KEY', '')

    run_id = str(uuid.uuid4())
    _ss_exec(
        'INSERT INTO hub_workflow_runs (id,workflow_id,user_id,status,input_data) VALUES (?,?,?,?,?)',
        (run_id, wid, user['id'], 'running', input_data))
    _ss_exec('UPDATE hub_workflows SET total_runs=total_runs+1 WHERE id=?', (wid,))

    def generate():
        engine          = HubWorkflowEngine(api_key, user, run_id, wid, wf.get('name', ''))
        log             = []
        final_output    = ''
        wf_tokens       = 0
        wf_input_tokens = 0
        wf_output_tokens= 0
        paused          = False

        yield json.dumps({'type': 'run_id', 'run_id': run_id}) + '\n'

        for chunk in engine.execute(wf, input_data):
            try:
                parsed = json.loads(chunk.strip())
                log.append(parsed)
                if parsed.get('type') == 'wf_complete':
                    final_output     = parsed.get('final_output', '')
                    wf_tokens        = parsed.get('total_tokens',  0) or 0
                    wf_input_tokens  = parsed.get('input_tokens',  0) or 0
                    wf_output_tokens = parsed.get('output_tokens', 0) or 0
                elif parsed.get('type') == 'wf_paused':
                    paused           = True
                    final_output     = parsed.get('final_output', '')
            except Exception:
                pass
            yield chunk

        run_status = 'waiting_approval' if paused else 'completed'
        _ss_exec(
            'UPDATE hub_workflow_runs SET status=?,output_data=?,execution_log_json=?,completed_at=? WHERE id=?',
            (run_status, final_output, json.dumps(log[-20:]),
             datetime.utcnow().isoformat(), run_id))

        # Token usage is now recorded per-node (inside HubWorkflowEngine._execute_nodes)
        # so each agent's cost is priced against the model it actually ran on —
        # a single aggregate row here couldn't be, since a workflow can mix models.

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')


@agents_hub_bp.route('/api/agenthub/workflows/runs', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_list_wf_runs():
    rows = _ss_exec(
        'SELECT TOP 20 * FROM hub_workflow_runs ORDER BY started_at DESC', fetchall=True) or []
    return jsonify(_fix_rows(rows))


@agents_hub_bp.route('/api/agenthub/workflows/<wid>/resume', methods=['POST'])
@auth.login_required
def hub_resume_workflow(wid):
    """Stream the continuation of a workflow that was paused at an approval node."""
    if not _APPROVAL_ENABLED:
        abort(404)
    user        = auth.current_user()
    if not _can_access_workflow(user, wid):
        return jsonify({'error': 'Access denied'}), 403
    data        = request.json or {}
    approval_id = data.get('approval_id', '')

    if not approval_id:
        return jsonify({'error': 'approval_id required'}), 400

    appr = _ss_exec(
        'SELECT status, workflow_id, request_type FROM hub_approvals WHERE approval_id=?',
        (approval_id,), fetchone=True)
    if not appr:
        return jsonify({'error': 'Approval not found'}), 404
    if appr['workflow_id'] != wid:
        return jsonify({'error': 'Approval does not belong to this workflow'}), 403
    if appr['status'] == 'pending':
        return jsonify({'error': 'Approval is still pending — wait for it to be resolved first'}), 409

    approved = (appr['status'] == 'approved')

    wf_row = _ss_exec('SELECT * FROM hub_workflows WHERE id=?', (wid,), fetchone=True)
    if not wf_row:
        return jsonify({'error': 'Workflow not found'}), 404
    wf = _wf_dict(wf_row)

    api_key      = os.environ.get('OPENAI_API_KEY', '')
    resume_run_id = str(uuid.uuid4())

    _ss_exec(
        'INSERT INTO hub_workflow_runs (id, workflow_id, user_id, status, input_data) VALUES (?,?,?,?,?)',
        (resume_run_id, wid, user['id'], 'running',
         f'resumed:approval:{approval_id}'))
    _ss_exec('UPDATE hub_workflows SET total_runs=total_runs+1 WHERE id=?', (wid,))

    def generate():
        engine           = HubWorkflowEngine(api_key, user, resume_run_id, wid, wf.get('name', ''))
        log              = []
        final_output     = ''
        wf_tokens        = 0
        wf_input_tokens  = 0
        wf_output_tokens = 0

        yield json.dumps({'type': 'run_id', 'run_id': resume_run_id}) + '\n'

        for chunk in engine.resume(wf, approval_id, approved):
            try:
                parsed = json.loads(chunk.strip())
                log.append(parsed)
                if parsed.get('type') == 'wf_complete':
                    final_output     = parsed.get('final_output', '')
                    wf_tokens        = parsed.get('total_tokens',  0) or 0
                    wf_input_tokens  = parsed.get('input_tokens',  0) or 0
                    wf_output_tokens = parsed.get('output_tokens', 0) or 0
            except Exception:
                pass
            yield chunk

        status = 'rejected' if not approved else 'completed'
        _ss_exec(
            'UPDATE hub_workflow_runs SET status=?,output_data=?,execution_log_json=?,completed_at=? WHERE id=?',
            (status, final_output, json.dumps(log[-20:]),
             datetime.utcnow().isoformat(), resume_run_id))

        # Token usage is now recorded per-node (inside HubWorkflowEngine._execute_nodes)
        # so each agent's cost is priced against the model it actually ran on.

    return Response(stream_with_context(generate()), mimetype='application/x-ndjson')
