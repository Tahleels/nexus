"""Human-in-the-loop approval routes for agents_hub_bp (workflow-resume
lives in agents_hub_workflows_routes.py, not here — see that file's
docstring). Split out of agents_hub_bp.py in Phase 3 Slice 4.
"""

import sys, os, uuid, json, logging, threading, re, calendar
from datetime import datetime, timedelta, date
from flask import (render_template, jsonify, request,
                   Response, stream_with_context, redirect, url_for, abort)
import auth
import token_limits
from agents_hub_bp import agents_hub_bp
from agents_hub_bp import _ss_exec, _fix_row, _fix_rows, _is_dev_or_admin, _is_admin, _APPROVAL_ENABLED


def _approval_dict(row: dict) -> dict:
    """Convert a raw hub_approvals row into an API-ready dict (parses context_json)."""
    if not row:
        return row
    row = _fix_row(dict(row))
    try:
        row['context'] = json.loads(row.get('context_json') or '{}')
    except Exception:
        row['context'] = {}
    return row


@agents_hub_bp.route('/api/agenthub/approvals', methods=['GET'])
@auth.login_required
def hub_list_approvals():
    if not _APPROVAL_ENABLED:
        abort(404)
    user    = auth.current_user()
    dept_id = request.args.get('dept_id',    type=int)
    proj_id = request.args.get('project_id', type=int)

    # Base JOIN includes agent/workflow dept so we can filter
    join_base = """
        SELECT ha.*,
               urb.username AS requested_by_username,
               uat.username AS assigned_to_username,
               uap.username AS approver_username,
               ag.name      AS agent_name,
               wf.name      AS workflow_name,
               ag.department_id AS agent_dept_id,
               ag.project_id    AS agent_proj_id,
               wf.department_id AS wf_dept_id,
               wf.project_id    AS wf_proj_id
        FROM   hub_approvals ha
        LEFT JOIN users urb ON urb.id = ha.requested_by_user_id
        LEFT JOIN users uat ON uat.id = ha.assigned_to_user_id
        LEFT JOIN users uap ON uap.id = ha.approver_user_id
        LEFT JOIN hub_agents ag ON ag.id = ha.agent_id
        LEFT JOIN hub_workflows wf ON wf.id = ha.workflow_id
    """

    if _is_admin(user):
        rows = _ss_exec(join_base + " ORDER BY ha.created_at DESC", fetchall=True) or []
    elif _is_dev_or_admin(user):
        rows = _ss_exec(
            join_base + " WHERE (ha.assigned_to_user_id IS NULL OR ha.assigned_to_user_id = ?) ORDER BY ha.created_at DESC",
            (user['id'],), fetchall=True) or []
    else:
        rows = _ss_exec(
            join_base + " WHERE ha.requested_by_user_id=? OR ha.assigned_to_user_id=? ORDER BY ha.created_at DESC",
            (user['id'], user['id']), fetchall=True) or []

    # Apply dept/project filter when accessed through a dept sub-tab
    if dept_id:
        rows = [r for r in rows if r.get('agent_dept_id') == dept_id or r.get('wf_dept_id') == dept_id]
    elif proj_id:
        rows = [r for r in rows if r.get('agent_proj_id') == proj_id or r.get('wf_proj_id') == proj_id]

    return jsonify([_approval_dict(r) for r in rows])


@agents_hub_bp.route('/api/agenthub/approvals/pending-count', methods=['GET'])
@auth.login_required
def hub_approval_pending_count():
    if not _APPROVAL_ENABLED:
        return jsonify({'count': 0})
    user = auth.current_user()
    if _is_admin(user):
        row = _ss_exec(
            "SELECT COUNT(*) AS c FROM hub_approvals WHERE status='pending'", fetchone=True)
    else:
        row = _ss_exec(
            "SELECT COUNT(*) AS c FROM hub_approvals WHERE status='pending' "
            "AND (assigned_to_user_id IS NULL OR assigned_to_user_id=?)",
            (user['id'],), fetchone=True)
    return jsonify({'count': (row or {}).get('c', 0)})


@agents_hub_bp.route('/api/agenthub/approvals/<approval_id>', methods=['GET'])
@auth.login_required
def hub_get_approval(approval_id):
    if not _APPROVAL_ENABLED:
        abort(404)
    user = auth.current_user()
    row  = _ss_exec("""
        SELECT ha.*,
               urb.username AS requested_by_username,
               uat.username AS assigned_to_username,
               uap.username AS approver_username,
               ag.name      AS agent_name,
               wf.name      AS workflow_name
        FROM   hub_approvals ha
        LEFT JOIN users urb ON urb.id = ha.requested_by_user_id
        LEFT JOIN users uat ON uat.id = ha.assigned_to_user_id
        LEFT JOIN users uap ON uap.id = ha.approver_user_id
        LEFT JOIN hub_agents ag ON ag.id = ha.agent_id
        LEFT JOIN hub_workflows wf ON wf.id = ha.workflow_id
        WHERE ha.approval_id = ?
    """, (approval_id,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    if (not _is_admin(user)
            and row.get('requested_by_user_id') != user['id']
            and row.get('assigned_to_user_id') not in (None, user['id'])):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(_approval_dict(row))


@agents_hub_bp.route('/api/agenthub/approvals/<approval_id>/resolve', methods=['POST'])
@auth.login_required
def hub_resolve_approval(approval_id):
    """Approve or reject a pending approval (POST /api/agenthub/approvals/<approval_id>/resolve).

    Only the designated approver (``assigned_to_user_id``) or an admin may
    resolve an approval left open to "any admin" (``assigned_to_user_id``
    is NULL). For workflow-type approvals, the response includes a
    ``resume_url`` the frontend should call to continue the paused workflow.

    Request JSON: ``decision`` ("approved"|"rejected"), ``note`` (required
    when rejecting).

    Returns:
        The updated approval dict, with ``resume_url``/``approval_id``/
        ``approved`` added for workflow approvals.
    """
    if not _APPROVAL_ENABLED:
        abort(404)
    user     = auth.current_user()
    data     = request.json or {}
    decision = data.get('decision')
    note     = data.get('note', '')

    if decision not in ('approved', 'rejected'):
        return jsonify({'error': 'decision must be approved or rejected'}), 400
    if decision == 'rejected' and not note:
        return jsonify({'error': 'note is required when rejecting'}), 400

    row = _ss_exec(
        "SELECT status, assigned_to_user_id FROM hub_approvals WHERE approval_id=?",
        (approval_id,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    if row['status'] != 'pending':
        return jsonify({'error': 'Already resolved'}), 409

    assigned = row.get('assigned_to_user_id')
    if not _is_admin(user) and assigned not in (None, user['id']):
        return jsonify({'error': 'You are not the designated approver'}), 403

    _ss_exec("""
        UPDATE hub_approvals
        SET status=?, approver_user_id=?, approver_note=?, resolved_at=GETDATE()
        WHERE approval_id=?
    """, (decision, user['id'], note or None, approval_id))

    updated = _ss_exec(
        "SELECT * FROM hub_approvals WHERE approval_id=?", (approval_id,), fetchone=True)
    result  = _approval_dict(updated)

    # ── Auto-trigger workflow resume for workflow-type approvals ──────────────
    updated_row = _fix_row(dict(updated)) if updated else {}
    if updated_row.get('request_type') == 'workflow' and updated_row.get('workflow_id'):
        wid_resume = updated_row['workflow_id']
        approved   = (decision == 'approved')
        result['resume_url'] = f'/api/agenthub/workflows/{wid_resume}/resume'
        result['approval_id'] = approval_id
        result['approved']    = approved

    return jsonify(result)


@agents_hub_bp.route('/api/agenthub/approvals/<approval_id>/status', methods=['GET'])
@auth.login_required
def hub_approval_status(approval_id):
    """Poll approval status — used by workflow builder to detect when to resume."""
    if not _APPROVAL_ENABLED:
        abort(404)
    row = _ss_exec(
        'SELECT approval_id, status, workflow_id, request_type FROM hub_approvals WHERE approval_id=?',
        (approval_id,), fetchone=True)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_fix_row(row))
