"""Hub Agents analytics/dashboard-stats + agent/workflow assignment
routes for agents_hub_bp. Split out of agents_hub_bp.py in Phase 3
Slice 4.
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
    get_assigned_agent_ids, get_assigned_workflow_ids, _agent_dict,
)


@agents_hub_bp.route('/api/agenthub/analytics', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_get_analytics():
    user       = auth.current_user()
    dept_id    = request.args.get('dept_id')
    project_id = request.args.get('project_id')

    # ── Date filter: quick range (today / 7d / all) OR a specific calendar
    # month of the current year (?month=YYYY-MM). A month value always wins
    # over ?range when both are present. Future months are rejected here too
    # (the UI already disables them), so a tampered request just falls back
    # to the default 7-day range instead of returning empty/garbage data.
    RANGE_DAYS = {'today': 0, '7d': 7}
    range_key  = request.args.get('range', '7d')
    month_key  = (request.args.get('month') or '').strip()

    start_date = end_date = None
    if month_key:
        try:
            y, m = (int(p) for p in month_key.split('-'))
            today = datetime.utcnow().date()
            not_future = (y, m) <= (today.year, today.month)
            if not_future and 1 <= m <= 12:
                start_date = date(y, m, 1)
                end_date   = date(y, m, calendar.monthrange(y, m)[1])
            else:
                month_key = ''
        except (ValueError, TypeError):
            month_key = ''

    if not month_key:
        if range_key != 'all' and range_key not in RANGE_DAYS:
            range_key = '7d'
        start_date = None if range_key == 'all' else \
            (datetime.utcnow() - timedelta(days=RANGE_DAYS[range_key])).date()
        end_date = None

    # AND-clause fragment for queries against token_usage (used inside an
    # existing WHERE ... ) and the params it needs, in order.
    _usage_conds, _date_params = [], []
    if start_date is not None:
        _usage_conds.append("CAST(t.used_at AS DATE) >= ?"); _date_params.append(start_date)
    if end_date is not None:
        _usage_conds.append("CAST(t.used_at AS DATE) <= ?"); _date_params.append(end_date)
    date_pred = ("AND " + " AND ".join(_usage_conds)) if _usage_conds else ""

    # Non-admins (devs) may only query a dept/project they belong to
    if not _is_admin(user):
        from database.org_db import _user_in_scope
        try:
            if dept_id and not _user_in_scope(user['id'], 'department', int(dept_id)):
                return jsonify({'error': 'Access denied'}), 403
            if project_id and not _user_in_scope(user['id'], 'project', int(project_id)):
                return jsonify({'error': 'Access denied'}), 403
        except ValueError:
            return jsonify({'error': 'Invalid department or project ID'}), 400

    # ── Agents (optionally filtered by dept/project) — include model for costing
    if dept_id:
        agents = _ss_exec(
            'SELECT name,avatar_color,model FROM hub_agents WHERE department_id=?',
            (dept_id,), fetchall=True) or []
    elif project_id:
        agents = _ss_exec(
            'SELECT name,avatar_color,model FROM hub_agents WHERE project_id=?',
            (project_id,), fetchall=True) or []
    else:
        agents = _ss_exec(
            'SELECT name,avatar_color,model FROM hub_agents', fetchall=True) or []

    # Current model per agent name — used only as a fallback for legacy
    # token_usage rows recorded before per-call model tracking existed.
    # Historical rows always prefer their own recorded t.model over this,
    # since an agent's model setting can change after the fact.
    agent_model_by_name = {r.get('name'): (r.get('model') or 'gpt-4o') for r in agents}

    tools    = _ss_exec(
        'SELECT display_name,name,total_calls,success_calls,category FROM hub_tools ORDER BY total_calls DESC',
        fetchall=True) or []
    kb_count = (_ss_exec('SELECT COUNT(*) AS c FROM hub_knowledge_bases', fetchone=True) or {}).get('c', 0)

    # ── Workflow runs — respects the same date filter ────────────────────────────
    _wf_conds, _wf_params = [], []
    if start_date is not None:
        _wf_conds.append("CAST(started_at AS DATE) >= ?"); _wf_params.append(start_date)
    if end_date is not None:
        _wf_conds.append("CAST(started_at AS DATE) <= ?"); _wf_params.append(end_date)
    wf_date_pred = ("WHERE " + " AND ".join(_wf_conds)) if _wf_conds else ""
    wf_args      = [tuple(_wf_params)] if _wf_params else []
    wf_count     = (_ss_exec(
        f'SELECT COUNT(*) AS c FROM hub_workflow_runs {wf_date_pred}',
        *wf_args, fetchone=True) or {}).get('c', 0)
    recent_runs  = _ss_exec(
        f'SELECT TOP 10 * FROM hub_workflow_runs {wf_date_pred} ORDER BY started_at DESC',
        *wf_args, fetchall=True) or []

    # ── User usage: I/O token split + model-aware exact cost, scoped to the
    # selected date range. Grouped by t.model (not just user/agent) so a user
    # who used the same agent under two different models over time gets each
    # slice priced correctly, rather than one blended rate for the whole
    # bucket. input_tokens / output_tokens are 0 for records pre-dating the
    # schema change; tokens_used + the blended model_cost_rate fallback is
    # used for those legacy rows.
    _usage_cols = """
        u.id AS uid, u.username, ISNULL(a.name, t.agent_name) AS agent_name,
        ISNULL(t.model, '')            AS call_model,
        ISNULL(SUM(t.tokens_used),0)   AS tokens,
        ISNULL(SUM(t.input_tokens),0)  AS input_tokens,
        ISNULL(SUM(t.output_tokens),0) AS output_tokens,
        ISNULL(MAX(a.model), 'gpt-4o') AS agent_model
    """
    # Match on the stable agent_id when the row has one (recorded going
    # forward); fall back to the mutable agent_name only for legacy rows
    # recorded before agent_id tracking existed. Without this, renaming a
    # hub agent would silently drop its historical usage from any
    # name-keyed join — including the department/project filters below,
    # which is why those now filter in the WHERE clause against this LEFT
    # JOIN instead of requiring an INNER JOIN match in the ON clause.
    _agent_join_sql = """
        LEFT JOIN hub_agents a
            ON a.id = t.agent_id
           OR (t.agent_id IS NULL AND a.name = t.agent_name)
    """

    def _usage_where(extra_cond=""):
        return f"""
            WHERE  t.call_type IN ('hub_chat','hub_workflow')
              {extra_cond}
              {date_pred}
            GROUP  BY u.id, u.username, ISNULL(a.name, t.agent_name), t.model
            ORDER  BY tokens DESC
        """

    if dept_id:
        _params = (dept_id,) + tuple(_date_params)
        raw_usage = _ss_exec(f"""
            SELECT {_usage_cols}
            FROM   token_usage t
            INNER JOIN users u ON u.id=t.user_id
            {_agent_join_sql}
            {_usage_where("AND a.department_id=?")}
        """, _params, fetchall=True) or []
    elif project_id:
        _params = (project_id,) + tuple(_date_params)
        raw_usage = _ss_exec(f"""
            SELECT {_usage_cols}
            FROM   token_usage t
            INNER JOIN users u ON u.id=t.user_id
            {_agent_join_sql}
            {_usage_where("AND a.project_id=?")}
        """, _params, fetchall=True) or []
    else:
        _args = [tuple(_date_params)] if _date_params else []
        raw_usage = _ss_exec(f"""
            SELECT {_usage_cols}
            FROM   token_usage t
            INNER JOIN users u ON u.id=t.user_id
            {_agent_join_sql}
            {_usage_where()}
        """, *_args, fetchall=True) or []

    # Aggregate per-user using exact I/O split where available
    user_map = {}
    for row in (raw_usage or []):
        uid   = row.get('uid')
        toks  = row.get('tokens') or 0
        i_tok = row.get('input_tokens') or 0
        o_tok = row.get('output_tokens') or 0
        aname = row.get('agent_name') or ''
        # Prefer the model actually recorded on the call; only fall back to
        # the agent's current model setting for legacy rows recorded before
        # per-call model tracking existed (an agent's model can change after
        # the fact, so using the current setting for real rows would misprice
        # historical usage).
        model = row.get('call_model') or row.get('agent_model') or 'gpt-4o'
        # Exact cost when I/O available; blended fallback for legacy rows
        if i_tok or o_tok:
            cost = token_limits.compute_cost(model, i_tok, o_tok)
        else:
            cost = toks * token_limits.model_cost_rate(model)
        if uid not in user_map:
            user_map[uid] = {'username': row.get('username', ''),
                             'tokens': 0, 'input_tokens': 0, 'output_tokens': 0,
                             'cost_usd': 0.0, 'agents': {}}
        user_map[uid]['tokens']        += toks
        user_map[uid]['input_tokens']  += i_tok
        user_map[uid]['output_tokens'] += o_tok
        user_map[uid]['cost_usd']      += cost
        if aname and toks:
            prev = user_map[uid]['agents'].get(aname, {
                'tokens': 0, 'input_tokens': 0, 'output_tokens': 0,
                'cost_usd': 0.0, 'model': model})
            prev['tokens']        += toks
            prev['input_tokens']  += i_tok
            prev['output_tokens'] += o_tok
            prev['cost_usd']      += cost
            user_map[uid]['agents'][aname] = prev

    user_usage = sorted([
        {
            'username':      v['username'],
            'tokens':        v['tokens'],
            'input_tokens':  v['input_tokens'],
            'output_tokens': v['output_tokens'],
            'cost_usd':      round(v['cost_usd'], 4),
            'agents': sorted(
                [{'name': n, 'tokens': d['tokens'],
                  'input_tokens': d['input_tokens'], 'output_tokens': d['output_tokens'],
                  'cost_usd': round(d['cost_usd'], 4), 'model': d['model']}
                 for n, d in v['agents'].items()],
                key=lambda x: -x['tokens'],
            ),
        }
        for v in user_map.values()
    ], key=lambda x: -x['tokens'])

    # ── Agent stats: runs/tokens/I-O split come from token_usage (call_type=
    # 'hub_chat') so the numbers are scoped to the same date range as User
    # Usage and Totals below — hub_agents.total_runs/total_tokens are lifetime,
    # non-resettable counters and can't be filtered by date.
    agent_usage_map = {}
    if agents:
        # Same stable-ID-first join as the user-usage query above — a
        # renamed hub agent still matches via agent_id, with the fragile
        # name match kept only as a fallback for pre-migration rows.
        _agent_join = """
            LEFT JOIN hub_agents a
                ON a.id = t.agent_id
               OR (t.agent_id IS NULL AND a.name = t.agent_name)
        """
        if dept_id:
            _agent_where_extra = "AND a.department_id = ?"
            _agent_params = [dept_id]
        elif project_id:
            _agent_where_extra = "AND a.project_id = ?"
            _agent_params = [project_id]
        else:
            _agent_where_extra = ""
            _agent_params = []
        _agent_params.extend(_date_params)

        # Grouped by t.model too (not just agent name) so each model an
        # agent has ever run under gets priced at its own rate — an agent's
        # `model` setting can change after the fact, so pricing everything
        # at the agent's *current* model would misprice historical rows.
        agent_rows = _ss_exec(f"""
            SELECT ISNULL(a.name, t.agent_name)  AS agent_name,
                   ISNULL(t.model, '')            AS call_model,
                   COUNT(*)                       AS runs,
                   ISNULL(SUM(t.tokens_used),0)   AS tokens,
                   ISNULL(SUM(t.input_tokens),0)  AS input_tokens,
                   ISNULL(SUM(t.output_tokens),0) AS output_tokens
            FROM   token_usage t
            {_agent_join}
            WHERE  t.call_type='hub_chat'
                   {_agent_where_extra}
                   {date_pred}
            GROUP  BY ISNULL(a.name, t.agent_name), t.model
        """, tuple(_agent_params), fetchall=True) or []

        for r in agent_rows:
            name  = r.get('agent_name')
            toks  = r.get('tokens') or 0
            i_tok = r.get('input_tokens') or 0
            o_tok = r.get('output_tokens') or 0
            model = r.get('call_model') or agent_model_by_name.get(name) or 'gpt-4o'
            if i_tok or o_tok:
                cost = token_limits.compute_cost(model, i_tok, o_tok)
            else:
                cost = toks * token_limits.model_cost_rate(model)
            bucket = agent_usage_map.setdefault(name, {
                'runs': 0, 'tokens': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0,
            })
            bucket['runs']          += r.get('runs') or 0
            bucket['tokens']        += toks
            bucket['input_tokens']  += i_tok
            bucket['output_tokens'] += o_tok
            bucket['cost_usd']      += cost

    agent_stats  = []
    total_cost   = 0.0
    total_tokens = 0
    total_runs   = 0
    for r in agents:
        name  = r.get('name', '')
        model = r.get('model') or 'gpt-4o'   # display only — actual cost is priced per-row above
        u     = agent_usage_map.get(name, {})
        runs  = u.get('runs') or 0
        toks  = u.get('tokens') or 0
        i_tok = u.get('input_tokens') or 0
        o_tok = u.get('output_tokens') or 0
        cost  = round(u.get('cost_usd') or 0.0, 4)
        total_cost   += cost
        total_tokens += toks
        total_runs   += runs
        agent_stats.append({
            'name':          name,
            'runs':          runs,
            'tokens':        toks,
            'input_tokens':  i_tok,
            'output_tokens': o_tok,
            'cost_usd':      cost,
            'model':         model,
            'color':         r.get('avatar_color') or '#6366f1',
        })
    agent_stats.sort(key=lambda a: -a['tokens'])

    return jsonify({
        'agent_stats':   agent_stats,
        'tool_stats':    [{'name': r['display_name'] or r['name'], 'calls': r['total_calls'],
                           'success': r['success_calls'], 'category': r['category']}
                          for r in tools],
        'totals':        {'tokens': total_tokens, 'agent_runs': total_runs,
                          'workflow_runs': wf_count, 'knowledge_items': kb_count,
                          'cost_usd': round(total_cost, 4)},
        'workflow_runs': _fix_rows(recent_runs),
        'user_usage':    user_usage,
        'range':         range_key if not month_key else None,
        'month':         month_key or None,
    })


@agents_hub_bp.route('/api/agenthub/dashboard/stats', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def hub_dashboard_stats():
    agents_count = (_ss_exec('SELECT COUNT(*) AS c FROM hub_agents', fetchone=True) or {}).get('c', 0)
    convos_count = (_ss_exec('SELECT COUNT(*) AS c FROM hub_conversations', fetchone=True) or {}).get('c', 0)
    wf_count     = (_ss_exec('SELECT COUNT(*) AS c FROM hub_workflows', fetchone=True) or {}).get('c', 0)
    jobs_count   = (_ss_exec('SELECT COUNT(*) AS c FROM hub_jobs', fetchone=True) or {}).get('c', 0)
    tools_count  = (_ss_exec('SELECT COUNT(*) AS c FROM hub_tools', fetchone=True) or {}).get('c', 0)
    totals_row   = _ss_exec(
        'SELECT SUM(total_tokens) AS t, SUM(total_runs) AS r FROM hub_agents', fetchone=True) or {}
    total_tokens = totals_row.get('t') or 0
    total_runs   = totals_row.get('r') or 0

    recent_convos = _ss_exec("""
        SELECT TOP 5 c.id, c.agent_id, c.title, c.created_at, c.updated_at,
            (SELECT COUNT(*) FROM hub_messages m WHERE m.conversation_id=c.id) AS message_count
        FROM hub_conversations c ORDER BY c.updated_at DESC
    """, fetchall=True) or []

    top_tools  = _ss_exec(
        'SELECT TOP 6 * FROM hub_tools ORDER BY total_calls DESC', fetchall=True) or []
    agent_list = _ss_exec(
        'SELECT TOP 6 * FROM hub_agents ORDER BY total_runs DESC', fetchall=True) or []
    recent_runs = _ss_exec(
        'SELECT TOP 5 * FROM hub_workflow_runs ORDER BY started_at DESC', fetchall=True) or []

    return jsonify({
        'stats': {
            'agents': agents_count, 'conversations': convos_count,
            'workflows': wf_count,  'jobs': jobs_count,
            'tools': tools_count,   'total_tokens': total_tokens, 'total_runs': total_runs,
        },
        'recent_conversations': _fix_rows(recent_convos),
        'top_tools':            _fix_rows(top_tools),
        'agents':               [_agent_dict(a) for a in agent_list],
        'recent_workflow_runs': _fix_rows(recent_runs),
        'system_status':        {'status': 'healthy', 'api': 'connected'},
    })


@agents_hub_bp.route('/api/agenthub/assignments/agents', methods=['GET'])
@auth.login_required
@auth.admin_required
def hub_get_agent_assignments():
    rows = _ss_exec("""
        SELECT haa.id, haa.user_id, haa.agent_id, haa.agent_name,
               haa.assigned_at, haa.assigned_by,
               u.username, u2.username AS assigned_by_name
        FROM  hub_agent_assignments haa
        JOIN  users u  ON u.id  = haa.user_id
        JOIN  users u2 ON u2.id = haa.assigned_by
        ORDER BY haa.assigned_at DESC
    """, fetchall=True) or []
    return jsonify(_fix_rows(rows))


@agents_hub_bp.route('/api/agenthub/assignments/agents', methods=['POST'])
@auth.login_required
@auth.admin_required
def hub_assign_agent():
    admin      = auth.current_user()
    d          = request.json or {}
    user_id    = d.get('user_id')
    agent_id   = d.get('agent_id')
    agent_name = d.get('agent_name', '')
    if not user_id or not agent_id:
        return jsonify({'error': 'user_id and agent_id required'}), 400
    try:
        _ss_exec("""
            IF NOT EXISTS (SELECT 1 FROM hub_agent_assignments WHERE user_id=? AND agent_id=?)
                INSERT INTO hub_agent_assignments (user_id, agent_id, agent_name, assigned_by)
                VALUES (?, ?, ?, ?)
        """, (user_id, agent_id, user_id, agent_id, agent_name, admin['id']))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@agents_hub_bp.route('/api/agenthub/assignments/agents/<int:assignment_id>', methods=['DELETE'])
@auth.login_required
@auth.admin_required
def hub_remove_agent_assignment(assignment_id):
    _ss_exec("DELETE FROM hub_agent_assignments WHERE id=?", (assignment_id,))
    return jsonify({'success': True})


@agents_hub_bp.route('/api/agenthub/assignments/workflows', methods=['GET'])
@auth.login_required
@auth.admin_required
def hub_get_workflow_assignments():
    rows = _ss_exec("""
        SELECT hwa.id, hwa.user_id, hwa.workflow_id, hwa.workflow_name,
               hwa.assigned_at, hwa.assigned_by,
               u.username, u2.username AS assigned_by_name
        FROM  hub_workflow_assignments hwa
        JOIN  users u  ON u.id  = hwa.user_id
        JOIN  users u2 ON u2.id = hwa.assigned_by
        ORDER BY hwa.assigned_at DESC
    """, fetchall=True) or []
    return jsonify(_fix_rows(rows))


@agents_hub_bp.route('/api/agenthub/assignments/workflows', methods=['POST'])
@auth.login_required
@auth.admin_required
def hub_assign_workflow():
    admin         = auth.current_user()
    d             = request.json or {}
    user_id       = d.get('user_id')
    workflow_id   = d.get('workflow_id')
    workflow_name = d.get('workflow_name', '')
    if not user_id or not workflow_id:
        return jsonify({'error': 'user_id and workflow_id required'}), 400
    try:
        _ss_exec("""
            IF NOT EXISTS (SELECT 1 FROM hub_workflow_assignments WHERE user_id=? AND workflow_id=?)
                INSERT INTO hub_workflow_assignments (user_id, workflow_id, workflow_name, assigned_by)
                VALUES (?, ?, ?, ?)
        """, (user_id, workflow_id, user_id, workflow_id, workflow_name, admin['id']))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@agents_hub_bp.route('/api/agenthub/assignments/workflows/<int:assignment_id>', methods=['DELETE'])
@auth.login_required
@auth.admin_required
def hub_remove_workflow_assignment(assignment_id):
    _ss_exec("DELETE FROM hub_workflow_assignments WHERE id=?", (assignment_id,))
    return jsonify({'success': True})
