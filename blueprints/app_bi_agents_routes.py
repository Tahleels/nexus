"""blueprints/app_bi_agents_routes.py — BI agent CRUD/schema/chat/sessions/conversations/execute-sql routes

Relocated from app.py verbatim (Phase 3 Slice 2 route reorganization).
Route functions are nested inside register_bi_agents_routes(...) exactly
like blueprints/app_jobs_routes.py::register_jobs_routes does, so they
register directly onto the real `app` object with their EXACT original
endpoint names and URL paths (no blueprint prefix) — existing url_for()
calls and hardcoded fetch() URLs throughout the app keep working unchanged.
"""
from flask import render_template, jsonify, request, redirect, url_for, Response, make_response, g
import json
import json as _json
import math
import time
import uuid
import threading
import pandas as pd
from datetime import datetime, date
import config
import auth
import token_limits
import org_db
import nexus_sync_db
from logging_config import get_logger
from app_route_helpers import _build_bi_guardrail_prefix
import bi_conversations_db as _bi_conv_db
import bi_training_db as _bi_td

logger = get_logger(__name__)


def register_bi_agents_routes(app, nlq_engine, agent_manager, db_manager):
    """Register BI agent management + chat routes onto app.

    Args:
        app: The Flask application instance.
    """

    @app.route('/api/bi-agents', methods=['GET', 'POST', 'DELETE', 'PUT'])
    @auth.login_required
    def bi_agents_management():
        """CRUD for BI agents; GET applies role/org-based visibility filtering and optional dept/project context."""
        if request.method == 'GET':
            user = auth.current_user()
            agents_list = agent_manager.load_agents()

            # Enrich with org context (many-to-many via junction tables)
            try:
                agent_names = [a.get('name') for a in agents_list if a.get('name')]
                org_map     = org_db.get_resource_orgs_batch('app_agent', agent_names)
                depts       = {d['id']: d for d in org_db.get_all_departments()}
                projects    = {p['id']: p for p in org_db.get_all_projects()}
                for a in agents_list:
                    orgs     = org_map.get(a.get('name'), {})
                    dept_ids = orgs.get('dept_ids', [])
                    proj_ids = orgs.get('project_ids', [])
                    a['dept_ids']      = dept_ids
                    a['project_ids']   = proj_ids
                    a['dept_names']    = [depts[d]['name']  for d in dept_ids if d in depts]
                    a['dept_colors']   = [depts[d]['color'] for d in dept_ids if d in depts]
                    a['project_names'] = [projects[p]['name'] for p in proj_ids if p in projects]
                    a['department_id'] = dept_ids[0] if dept_ids else None
                    a['project_id']    = proj_ids[0] if proj_ids else None
                    a['dept_name']     = a['dept_names'][0]    if a['dept_names']    else None
                    a['dept_color']    = a['dept_colors'][0]   if a['dept_colors']   else None
                    a['project_name']  = a['project_names'][0] if a['project_names'] else None
            except Exception:
                pass

            if user['role'] == 'dev':
                # Devs see agents they created OR that were explicitly assigned to them by an admin
                assigned = set(auth.get_assigned_agents(user['id']))
                agents_list = [a for a in agents_list
                               if a.get('created_by') == user['id'] or a.get('name') in assigned]
            elif user['role'] == 'user':
                assigned = auth.get_assigned_agents(user['id'])
                agents_list = [a for a in agents_list if a.get('name') in assigned]

            # Restrict non-admins to agents that belong to their own departments/projects
            # (agents with no dept/project assigned remain visible to everyone)
            user_dept_ids, user_proj_ids = set(), set()
            if user['role'] != 'admin':
                try:
                    org_ctx       = org_db.get_user_org_assignments(user['id'])
                    user_dept_ids = set(org_ctx.get('dept_ids') or [])
                    user_proj_ids = set(org_ctx.get('project_ids') or [])

                    def _in_user_scope(a):
                        a_depts = set(a.get('dept_ids') or [])
                        a_projs = set(a.get('project_ids') or [])
                        if not a_depts and not a_projs:
                            return True  # unscoped agent — visible to all
                        return bool(a_depts & user_dept_ids) or bool(a_projs & user_proj_ids)

                    agents_list = [a for a in agents_list if _in_user_scope(a)]
                except Exception:
                    pass

            # If a specific dept/project context was selected in the sidebar, narrow
            # the list to that single context (validated against the user's own
            # memberships for non-admins so they can't spoof access to other contexts)
            ctx_dept_id    = request.args.get('dept_id', type=int)
            ctx_project_id = request.args.get('project_id', type=int)
            if ctx_dept_id is not None or ctx_project_id is not None:
                if user['role'] != 'admin':
                    if ctx_dept_id is not None and ctx_dept_id not in user_dept_ids:
                        ctx_dept_id = None
                    if ctx_project_id is not None and ctx_project_id not in user_proj_ids:
                        ctx_project_id = None
                if ctx_dept_id is not None or ctx_project_id is not None:
                    def _matches_ctx(a):
                        if ctx_dept_id is not None and ctx_dept_id in (a.get('dept_ids') or []):
                            return True
                        if ctx_project_id is not None and ctx_project_id in (a.get('project_ids') or []):
                            return True
                        return False

                    agents_list = [a for a in agents_list if _matches_ctx(a)]

            return jsonify(agents_list)

        # POST / DELETE require dev or admin
        if auth.current_user()['role'] not in ('admin', 'dev'):
            return jsonify({"status": "error", "message": "Permission denied"}), 403
        elif request.method == 'POST':
            try:
                user = auth.current_user()
                data = request.get_json()
                required_fields = ['name', 'description', 'database_connection']
                for field in required_fields:
                    if field not in data:
                        return jsonify({"status": "error", "message": f"Missing required field: {field}"}), 400

                # Devs can only use database connections assigned to them by an admin
                if user['role'] == 'dev':
                    from app_db import get_dev_assigned_resource_ids
                    allowed_ids = set(get_dev_assigned_resource_ids(user['id'], 'db_connection'))
                    all_conns = db_manager.load_connections()
                    allowed_names = {c['name'] for c in all_conns if c.get('id') in allowed_ids}
                    if data['database_connection'] not in allowed_names:
                        return jsonify({"status": "error",
                                        "message": "You are not authorized to use that database connection"}), 403

                # Tag the agent with its creator so it auto-belongs to them
                data['created_by'] = user['id']

                success, message = agent_manager.create_agent(data)
                if success:
                    # Auto-assign the new agent to its creator in the legacy assignments table
                    auth.assign_agent(user['id'], data['name'], user['id'])
                    return jsonify({"status": "success", "message": message})
                else:
                    return jsonify({"status": "error", "message": message}), 400
            except Exception as e:
                logger.exception(" BI Agents Failed")
                return jsonify({"status": "error", "message": "Internal server error"}), 500
        elif request.method == 'DELETE':
            try:
                data = request.get_json()
                agent_name = data.get('name')
                if not agent_name:
                    return jsonify({"status": "error", "message": "Agent name is required"}), 400
                success, message = agent_manager.delete_agent(agent_name)
                if success:
                    # Cascade: remove any guardrail rows tied to this BI agent
                    try:
                        org_db.delete_bi_agent_guardrails(agent_name)
                    except Exception:
                        logger.warning(f"Failed to clean guardrails for deleted BI agent '{agent_name}'")
                    return jsonify({"status": "success", "message": message})
                else:
                    return jsonify({"status": "error", "message": message}), 400
            except Exception as e:
                logger.exception(" BI Agents Delete Failed")
                return jsonify({"status": "error", "message": "Internal server error"}), 500
        elif request.method == 'PUT':
            try:
                data = request.get_json()
                agent_name = data.get('name')
                if not agent_name:
                    return jsonify({"status": "error", "message": "Agent name is required"}), 400
                success, message = agent_manager.update_agent(agent_name, data)
                if success:
                    return jsonify({"status": "success", "message": message})
                else:
                    return jsonify({"status": "error", "message": message}), 400
            except Exception as e:
                logger.exception(" BI Agents Update Failed")
                return jsonify({"status": "error", "message": "Internal server error"}), 500


    @app.route('/api/bi-agents/schema', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def get_database_schema():
        """Return column/table schema info for a connection, optionally restricted to selected tables."""
        try:
            data = request.get_json()
            connection_name = data.get('connection_name')
            selected_tables = data.get('tables', [])
            logger.info(f" Schema request for connection: {connection_name}")
            if not connection_name:
                return jsonify({"status": "error", "message": "Connection name is required"}), 400
            schema_info, error = agent_manager.get_database_schema(connection_name, selected_tables)
            if error:
                logger.exception(" Schema error")
                return jsonify({"status": "error", "message": error}), 400
            return jsonify({"status": "success", "schema": schema_info})
        except Exception as e:
            logger.exception(" Unexpected error in schema endpoint")
            return jsonify({"status": "error", "message": f"Unexpected error: {str(e)}"}), 500


    @app.route('/api/bi-agents/generate-schema-context', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def generate_schema_context():
        """Generate (LLM-assisted) schema context for a BI agent and record the schema_analysis token cost."""
        try:
            data = request.get_json()
            connection_name = data.get('connection_name')
            selected_tables = data.get('tables', [])
            selected_columns = data.get('columns', {})
            logger.info(f" Generate Schema Context - Connection: {connection_name}")
            logger.info(f" Tables: {selected_tables}")
            logger.info(f" Columns: {selected_columns}")
            if not connection_name or not selected_tables:
                return jsonify({"status": "error", "message": "Connection name and tables are required"}), 400
            schema_context, error = agent_manager.generate_schema_context(
                connection_name, selected_tables, selected_columns
            )
            if error:
                logger.exception(" Error generating schema context")
                return jsonify({"status": "error", "message": error}), 400

            # ── Record schema_analysis tokens ────────────────────────────────────
            user = auth.current_user()
            ai_text  = (schema_context or {}).get("ai_analysis", "") or ""
            _s_in    = int((schema_context or {}).get("_schema_in_tokens",  0) or 0)
            _s_out   = int((schema_context or {}).get("_schema_out_tokens", 0) or 0)
            _s_total = _s_in + _s_out or None
            if ai_text:
                token_limits.record_tokens(
                    user          = user,
                    call_type     = "schema_analysis",
                    agent_name    = connection_name,
                    question      = f"schema analysis: {', '.join(selected_tables[:5])}",
                    response      = ai_text,
                    extra         = str(selected_columns)[:500],
                    actual_tokens = _s_total,
                    input_tokens  = _s_in,
                    output_tokens = _s_out,
                    model         = "gpt-3.5-turbo",
                )
            # ─────────────────────────────────────────────────────────────────────

            logger.info(f" Successfully generated schema context")
            return jsonify({"status": "success", "schema_context": schema_context})
        except Exception as e:
            logger.exception(" Unexpected error in generate-schema-context endpoint")
            return jsonify({"status": "error", "message": f"Unexpected error: {str(e)}"}), 500


    @app.route('/api/bi-agents/tables', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def get_database_tables():
        """List all tables available on a database connection."""
        try:
            data = request.get_json()
            connection_name = data.get('connection_name')
            logger.info(f" Table list request for: {connection_name}")
            if not connection_name:
                return jsonify({"status": "error", "message": "Connection name is required"}), 400
            tables, error = agent_manager.get_database_tables(connection_name)
            if error:
                return jsonify({"status": "error", "message": error}), 400
            return jsonify({"status": "success", "tables": tables})
        except Exception as e:
            logger.exception(" Error in tables endpoint")
            return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500


    @app.route('/api/bi-agents/table-columns', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def get_table_columns():
        """Return column details for the given table names on a database connection."""
        try:
            data = request.get_json()
            connection_name = data.get('connection_name')
            table_names = data.get('table_names', [])
            logger.info(f" Column request for {len(table_names)} tables")
            if not connection_name:
                return jsonify({"status": "error", "message": "Connection name is required"}), 400
            if not table_names:
                return jsonify({"status": "success", "tables": []})
            table_details, error = agent_manager.get_table_columns(connection_name, table_names)
            if error:
                return jsonify({"status": "error", "message": error}), 400
            return jsonify({"status": "success", "tables": table_details})
        except Exception as e:
            logger.exception(" Error in columns endpoint")
            return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500


    @app.route('/api/bi-agents/chat', methods=['POST'])
    @auth.login_required
    def agent_chat():
        """Main BI chat endpoint: enforces token quota, injects guardrails/identity context,
        runs the question through NLQEngine, persists the conversation, and logs BI training data."""
        try:
            data            = request.get_json()
            agent_name      = data.get('agent_name')
            question        = data.get('question')
            session_id      = data.get('session_id') or str(uuid.uuid4())
            conversation_id = data.get('conversation_id')
            # Active org scope forwarded by the frontend
            _bi_scope_type  = data.get('scope_type') or None   # 'department' | 'project' | None
            _bi_scope_id_v  = data.get('scope_id')
            _bi_scope_id    = int(_bi_scope_id_v) if _bi_scope_id_v is not None else None

            logger.info(f"[session={session_id}] Question received | agent={agent_name} | q={question}")

            if not agent_name or not question:
                return jsonify({"status": "error", "message": "Agent name and question are required"}), 400

            # ── TOKEN LIMIT CHECK ─────────────────────────────────────
            user = auth.current_user()
            allowed, limit_msg = token_limits.check_and_record(user, question, agent_name=agent_name)
            if not allowed:
                summary = token_limits.get_usage_summary(user)
                logger.warning(f"[token] Blocked: user={user['username']} used={summary['used_today']} limit={summary['limit']}")
                return jsonify({
                    "success":    False,
                    "error":      limit_msg,
                    "error_type": "token_limit_exceeded",
                    "token_usage": summary,
                }), 429
            # ─────────────────────────────────────────────────────────

            agent_config = agent_manager.get_agent(agent_name)
            if not agent_config:
                return jsonify({"status": "error", "message": "Agent not found"}), 404

            # ── Inject guardrails + user identity for BI agents ───────────────────
            # Fail-closed: if guardrail lookup raises (e.g. DB down), block the
            # request rather than running a query without access controls enforced.
            try:
                _guardrail  = org_db.get_agent_guardrail(user['id'], agent_name, 'bi',
                                                          active_scope_type=_bi_scope_type,
                                                          active_scope_id=_bi_scope_id)
                _user_org   = org_db.get_user_identity_context(user['id'])
                _bi_prefix  = _build_bi_guardrail_prefix(user, _user_org, _guardrail)
                if _bi_prefix:
                    question = _bi_prefix + '\n\nUser question: ' + question
            except Exception as _gex:
                logger.error(f'[bi-guardrail] access control check failed, blocking request: {_gex}')
                return jsonify({
                    "success":    False,
                    "error":      "Unable to verify access controls. Please try again.",
                    "error_type": "guardrail_failure",
                }), 500
            # Pass guardrail separately so the SQL chain can enforce it at runtime
            _user_ctx = {**user, '_guardrail': _guardrail or {}}
            # ─────────────────────────────────────────────────────────────────────

            # ── BI CONVERSATION PERSISTENCE — create/validate conversation ────────
            _raw_question = data.get('question', '')   # un-prefixed for display/storage
            if not conversation_id:
                _title = (_raw_question[:50] + '…') if len(_raw_question) > 50 else _raw_question
                conversation_id = _bi_conv_db.create_conversation(agent_name, user['id'], _title)
            # Save user message before calling the engine
            def _save_user_msg():
                _bi_conv_db.save_message(conversation_id, 'user', _raw_question)
            threading.Thread(target=_save_user_msg, daemon=True).start()
            # ─────────────────────────────────────────────────────────────────────

            result = nlq_engine.process_question(
                question=question,
                agent_config=agent_config,
                connection_name=agent_config['database_connection'],
                session_id=session_id,
                user_context=_user_ctx,
            )

            # ── RECORD TOKENS AFTER RESPONSE ─────────────────────────
            # The engine already recorded chat/insight/analysis/schema_prune tokens
            # internally via token_recorder.  We just need the updated summary.
            tokens_used = 0   # already tracked inside nlq_engine
            # Attach usage info to the response so the frontend can show it
            summary = token_limits.get_usage_summary(user)
            result['token_usage'] = summary
            # ─────────────────────────────────────────────────────────

            logger.info(f"[session={result.get('session_id')}] Response sent | tokens={tokens_used}")

            # ── BI CONVERSATION PERSISTENCE — save assistant response ─────────────
            _cid_final = conversation_id
            _res_copy  = dict(result)
            def _save_assistant_msg():
                _bi_conv_db.save_message(
                    _cid_final, 'assistant',
                    _res_copy.get('analysis') or '',
                    sql_query = _res_copy.get('sql_query') or '',
                )
                _bi_conv_db.update_timestamp(_cid_final)
            threading.Thread(target=_save_assistant_msg, daemon=True).start()
            result['conversation_id'] = conversation_id
            # ─────────────────────────────────────────────────────────────────────

            # ── BI TRAINING DATA ──────────────────────────────────────────────────
            if result.get("success") and not result.get("is_conversational"):
                _bi_td.save_async(
                    tool_type   = "bi_query",
                    user        = user,
                    instruction = question,
                    context     = json.dumps({
                        "agent_name":         agent_name,
                        "connection":         agent_config.get("database_connection", ""),
                        "schema_description": str(agent_config.get("schema_context") or "")[:1500],
                    }),
                    output      = json.dumps({
                        "sql_query": result.get("sql_query", ""),
                        "response":  result.get("response", ""),
                        "columns":   result.get("columns", []),
                    }, ensure_ascii=False),
                    sql_query   = result.get("sql_query", ""),
                    agent_name  = agent_name,
                    model_used  = agent_config.get("model", "gpt-4o"),
                    token_count = result.get("token_usage", {}).get("used_today"),
                    domain      = "bi_query",
                    tags        = agent_name,
                )
            # ─────────────────────────────────────────────────────────────────────

            return jsonify(result)

        except Exception as e:
            logger.exception(" Chat error")
            return jsonify({"success": False, "error": str(e), "response": f"Chat error: {str(e)}"}), 500


    @app.route('/api/bi-agents/sessions/<session_id>', methods=['GET', 'DELETE'])
    @auth.login_required
    def manage_session(session_id):
        """Get the in-memory chat history for a BI session, or delete it."""
        if request.method == 'GET':
            session_info = nlq_engine.session_manager.get_session(session_id)
            if session_info:
                formatted_info = {
                    "session_id": session_id,
                    "agent_name": session_info['agent_config']['name'],
                    "message_count": session_info['message_count'],
                    "chat_history": session_info['chat_history'],
                    "created_at": session_info['created_at'],
                    "last_activity": session_info['last_activity']
                }
                return jsonify({"status": "success", "session": formatted_info})
            else:
                return jsonify({"status": "error", "message": "Session not found"}), 404
        elif request.method == 'DELETE':
            if nlq_engine.session_manager.delete_session(session_id):
                return jsonify({"status": "success", "message": "Session deleted successfully"})
            else:
                return jsonify({"status": "error", "message": "Session not found"}), 404


    @app.route('/api/bi-agents/sessions', methods=['GET'])
    @auth.login_required
    def list_sessions():
        """List all active (in-memory) BI chat sessions with basic metadata."""
        active_sessions = nlq_engine.session_manager.get_active_sessions()
        sessions_info = []
        for session_id in active_sessions:
            session = nlq_engine.session_manager.get_session(session_id)
            if session:
                sessions_info.append({
                    "session_id": session_id,
                    "agent_name": session['agent_config']['name'],
                    "message_count": session['message_count'],
                    "created_at": session['created_at'],
                    "last_activity": session['last_activity']
                })
        return jsonify({"status": "success", "sessions": sessions_info})


    @app.route('/api/bi-agents/conversations', methods=['GET'])
    @auth.login_required
    def bi_list_conversations():
        """List persisted BI conversations for the current user (or all, for admin/dev), optionally by agent."""
        user       = auth.current_user()
        agent_name = request.args.get('agent_name', '')
        is_admin   = user.get('role') in ('admin', 'dev')
        rows = _bi_conv_db.list_conversations(
            user_id    = user['id'],
            agent_name = agent_name,
            is_admin   = is_admin,
        )
        return jsonify(rows)


    @app.route('/api/bi-agents/conversations', methods=['POST'])
    @auth.login_required
    def bi_create_conversation():
        """Create an empty BI conversation shell for an agent."""
        user  = auth.current_user()
        d     = request.get_json() or {}
        cid   = _bi_conv_db.create_conversation(
            agent_name = d.get('agent_name', ''),
            user_id    = user['id'],
            title      = d.get('title', 'New Conversation'),
        )
        return jsonify({'conversation_id': cid}), 201


    @app.route('/api/bi-agents/conversations/<cid>', methods=['GET'])
    @auth.login_required
    def bi_get_conversation(cid):
        """Return a single BI conversation with its full message history."""
        user     = auth.current_user()
        is_admin = user.get('role') in ('admin', 'dev')
        conv = _bi_conv_db.get_conversation(cid, user_id=user['id'], is_admin=is_admin)
        if not conv:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(conv)


    @app.route('/api/bi-agents/conversations/<cid>', methods=['DELETE'])
    @auth.login_required
    def bi_delete_conversation(cid):
        """Delete a BI conversation (owner or admin/dev only)."""
        user     = auth.current_user()
        is_admin = user.get('role') in ('admin', 'dev')
        ok = _bi_conv_db.delete_conversation(cid, user_id=user['id'], is_admin=is_admin)
        if not ok:
            return jsonify({'error': 'Not found or forbidden'}), 404
        return jsonify({'success': True})


    @app.route('/api/bi-agents/execute-sql', methods=['POST'])
    @auth.login_required
    def bi_execute_sql():
        """Run a raw SQL query (read or write) against a BI agent's connection and return up to 500 rows."""
        user       = auth.current_user()
        d          = request.get_json() or {}
        agent_name = d.get('agent_name', '')
        sql_query  = d.get('sql_query', '').strip()
        if not agent_name or not sql_query:
            return jsonify({'error': 'agent_name and sql_query required'}), 400

        agent_config = agent_manager.get_agent(agent_name)
        if not agent_config:
            return jsonify({'error': 'Agent not found'}), 404

        conn_name   = agent_config.get('database_connection', '')
        conn_config = nlq_engine.database_manager.get_connection(conn_name) if conn_name else None
        if not conn_config:
            return jsonify({'error': 'Connection not found'}), 404

        try:
            from sqlalchemy import text as sa_text
            engine  = nlq_engine.database_manager.get_engine(conn_config['name'])
            with engine.connect() as conn:
                res     = conn.execute(sa_text(sql_query))
                columns = list(res.keys())
                raw     = res.fetchmany(500)
            # Normalize row values to JSON-safe types (Decimal, datetime, date, etc.)
            def _safe(v):
                if v is None:
                    return None
                if isinstance(v, (int, float, bool, str)):
                    return v
                import decimal, datetime
                if isinstance(v, decimal.Decimal):
                    return float(v)
                if isinstance(v, (datetime.datetime, datetime.date)):
                    return v.isoformat()
                return str(v)
            rows = [{col: _safe(val) for col, val in zip(columns, row)} for row in raw]
            return jsonify({'success': True, 'columns': columns, 'data': rows, 'row_count': len(rows)})
        except Exception as exc:
            logger.error('bi_execute_sql: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 400

