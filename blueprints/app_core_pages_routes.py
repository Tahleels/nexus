"""blueprints/app_core_pages_routes.py — dashboard, bi-agents-pages, chat/connections/documents, system, clients routes

Relocated from app.py verbatim (Phase 3 Slice 2 route reorganization).
Route functions are nested inside register_core_pages_routes(...) exactly
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
import pandas as pd
from datetime import datetime, date
import config
import auth
import token_limits
import org_db
import nexus_sync_db
from logging_config import get_logger
import app_db as _app_db
from clients_db import (
    load_clients as _load_clients_db,
    list_clients as _list_clients_db,
    get_client as _get_client_db,
    save_client as _save_client_db,
    delete_client as _delete_client_db,
)

logger = get_logger(__name__)

_TASK_TRACKER_CONNECTION = "meeting_intel"


def register_core_pages_routes(app, nlq_engine, agent_manager, db_manager):
    """Register dashboard/bi-pages/connections/clients routes onto app.

    Args:
        app: The Flask application instance.
    """

    @app.route('/')
    @app.route('/dashboard')
    @auth.login_required
    def dashboard():
        """Render the main dashboard page."""
        return render_template('dashboard.html')


    @app.route('/api/dashboard/my-tasks/debug', methods=['GET'])
    @auth.login_required
    def dashboard_my_tasks_debug():
        """Debug endpoint — exposes every step of the my-tasks pipeline."""
        user = auth.current_user()
        local_user_id = user['id']
        out = {"local_user_id": local_user_id, "steps": {}}

        # Step 1: portal ID
        portal_user_id = nexus_sync_db.get_portal_user_id(local_user_id)
        out["steps"]["portal_user_id"] = portal_user_id
        if not portal_user_id:
            out["error"] = "No portal user mapping found in nexus_user_sync"
            return jsonify(out)

        # Step 2: project name map
        try:
            with _app_db.get_app_db() as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT mpm.nexus_project_id, mpm.nexus_project_name
                    FROM user_projects up
                    JOIN nexus_project_mapping mpm ON mpm.project_id = up.project_id
                    WHERE up.user_id = ?
                """, local_user_id)
                project_rows = c.fetchall()
            out["steps"]["project_name_map"] = {r[0]: r[1] for r in project_rows}
        except Exception as e:
            out["steps"]["project_name_map_error"] = str(e)

        # Step 3: scan all connections for TasksTracker table
        try:
            all_conns = db_manager.load_connections()
            out["steps"]["available_connections"] = []
            found_conn_name = request.args.get("conn", "")
            for c in all_conns:
                entry = {"name": c.get("name"), "has_tasks_tracker": False, "error": None}
                try:
                    test_conn = db_manager.get_connection_object(c)
                    cur2 = test_conn.cursor()
                    cur2.execute("SELECT TOP 1 Id FROM dbo.TasksTracker")
                    cur2.fetchone()
                    test_conn.close()
                    entry["has_tasks_tracker"] = True
                    if not found_conn_name:
                        found_conn_name = c.get("name")
                except Exception as ce:
                    entry["error"] = str(ce)[:120]
                out["steps"]["available_connections"].append(entry)
        except Exception as e:
            out["steps"]["available_connections_error"] = str(e)
            return jsonify(out)

        if not found_conn_name:
            out["error"] = "No connection found that contains dbo.TasksTracker"
            return jsonify(out)

        connection_cfg = db_manager.get_connection(found_conn_name)
        out["steps"]["connection_name"] = found_conn_name
        out["steps"]["connection_found"] = bool(connection_cfg)

        if not connection_cfg:
            out["error"] = f"Connection '{found_conn_name}' not found"
            return jsonify(out)

        # Step 4: raw query
        sql = """
            SELECT [Id], [Title], [DueDate], [Status], [Priority], [ProjectId]
            FROM dbo.[TasksTracker]
            WHERE [UserId] = ?
              AND [IsDeleted] = 0
              AND [Status] IN (1, 3)
            ORDER BY [DueDate] ASC
        """
        out["steps"]["sql"] = sql.strip()
        out["steps"]["sql_params"] = [portal_user_id]
        try:
            db_conn = db_manager.get_connection_object(connection_cfg)
            cur = db_conn.cursor()
            cur.execute(sql, [portal_user_id])
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            db_conn.close()
            out["steps"]["row_count"] = len(rows)
            out["steps"]["rows"] = [{k: str(v) for k, v in r.items()} for r in rows]
        except Exception as e:
            out["steps"]["query_error"] = str(e)

        return jsonify(out)


    @app.route('/api/dashboard/my-tasks', methods=['GET'])
    @auth.login_required
    def dashboard_my_tasks():
        user = auth.current_user()
        local_user_id = user['id']

        portal_user_id = nexus_sync_db.get_portal_user_id(local_user_id)
        if not portal_user_id:
            return jsonify({"tasks": [], "message": "No portal user mapping found"})

        try:
            with _app_db.get_app_db() as conn:
                c = conn.cursor()
                # Map nexus_project_id → local project or department name
                c.execute("""
                    SELECT mpm.nexus_project_id,
                           COALESCE(p.name, d.name, mpm.nexus_project_name) AS display_name
                    FROM nexus_project_mapping mpm
                    LEFT JOIN projects     p ON p.id = mpm.project_id
                    LEFT JOIN departments  d ON d.id = mpm.dept_id
                """)
                project_name_map = {r[0]: r[1] for r in c.fetchall()}
        except Exception as e:
            logger.warning("dashboard_my_tasks: project lookup failed: %s", e)
            project_name_map = {}

        try:
            connection_cfg = db_manager.get_connection(_TASK_TRACKER_CONNECTION)
            db_conn = db_manager.get_connection_object(connection_cfg)
            cur = db_conn.cursor()
            cur.execute("""
                SELECT [Id], [Title], [DueDate], [Status], [Priority], [ProjectId]
                FROM dbo.[TasksTracker]
                WHERE [UserId] = ?
                  AND [IsDeleted] = 0
                  AND [Status] = 1
                ORDER BY [DueDate] ASC
            """, [portal_user_id])
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            db_conn.close()

            today = date.today()
            for row in rows:
                due = row.get("DueDate")
                due_date = due.date() if isinstance(due, datetime) else due
                is_overdue = bool(due_date) and due_date < today
                row["StatusLabel"] = "Overdue" if is_overdue else "In Progress"
                row["IsOverdue"] = is_overdue
                row["ProjectName"] = project_name_map.get(int(row["ProjectId"]) if row.get("ProjectId") is not None else None, "")
                if row.get("DueDate"):
                    row["DueDate"] = str(row["DueDate"])
        except Exception as e:
            logger.warning("dashboard_my_tasks: query failed: %s", e)
            return jsonify({"tasks": [], "message": "Failed to fetch tasks"})

        return jsonify({"tasks": rows})


    @app.route('/api/dashboard/stats', methods=['GET'])
    @auth.login_required
    def dashboard_stats():
        """Return summary counts (BI agents, connections, docs, reasoning agents) for the main dashboard."""
        user = auth.current_user()

        # BI Agents
        agents_list = agent_manager.load_agents()
        if user['role'] in ('dev', 'user'):
            assigned = auth.get_assigned_agents(user['id'])
            agents_list = [a for a in agents_list if a.get('name') in assigned]

        # Connections
        connections = db_manager.load_connections()

        # Knowledge Docs (you can replace later with real storage)
        documents = []  # placeholder for now

        # Reasoning Agents (not built yet)
        reasoning_agents = []  # placeholder

        return jsonify({
            "bi_agents": {
                "count": len(agents_list),
                "items": agents_list[:3]
            },
            "connections": {
                "count": len(connections)
            },
            "knowledge_docs": {
                "count": len(documents)
            },
            "reasoning_agents": {
                "count": len(reasoning_agents)
            },
            "user": {
                "role": user['role']
            }
        })


    @app.route('/bi-agents')
    @auth.login_required
    def bi_agents():
        """Render the BI agents page."""
        return render_template('bi_agents.html')


    @app.route('/reasoning-agents')
    @auth.login_required
    def reasoning_agents():
        """Render the reasoning agents page."""
        return render_template('reasoning_agents.html')


    @app.route('/database-connections')
    @auth.login_required
    @auth.dev_or_admin_required
    def database_connections():
        """Render the database connections page, pre-loaded with existing connections."""
        connections = db_manager.load_connections()
        return render_template('database_connections.html', connections=connections)


    @app.route('/api/chat-agents', methods=['GET', 'POST'])
    @auth.login_required
    def chat_agents():
        """Placeholder endpoint — GET returns an empty list, POST returns a stub agent_id (not wired to real data)."""
        if request.method == 'GET':
            return jsonify([])
        else:
            data = request.get_json()
            return jsonify({"status": "success", "agent_id": 1})


    @app.route('/api/chat', methods=['POST'])
    @auth.login_required
    def chat():
        """Placeholder endpoint — always returns a static stub response (not wired to a real chat engine)."""
        data = request.get_json()
        return jsonify({"response": "This is a placeholder response"})


    @app.route('/api/connections', methods=['GET', 'POST', 'PUT', 'DELETE'])
    @auth.login_required
    @auth.dev_or_admin_required
    def connections():
        """CRUD for database connections; GET is scoped to a dev's assigned connections, others are unrestricted."""
        if request.method == 'GET':
            connections_list = db_manager.load_connections()
            user = auth.current_user()
            if user and user.get('role') == 'dev':
                from app_db import get_dev_assigned_resource_ids
                allowed_ids = set(get_dev_assigned_resource_ids(user['id'], 'db_connection'))
                connections_list = [c for c in connections_list if c.get('id') in allowed_ids]
            return jsonify(connections_list)
        elif request.method == 'POST':
            try:
                data = request.get_json()
                required_fields = ['name', 'type', 'server', 'port', 'username', 'password']
                for field in required_fields:
                    if field not in data:
                        return jsonify({"status": "error", "message": f"Missing required field: {field}"}), 400
                success, message = db_manager.add_connection(data)
                if success:
                    return jsonify({"status": "success", "message": message})
                else:
                    return jsonify({"status": "error", "message": message}), 400
            except Exception as e:
                logger.exception(f" Failed to create connection | name={data.get('name')}")
                return jsonify({"status": "error", "message": "Internal server error"}), 500
        elif request.method == 'PUT':
            try:
                data = request.get_json()
                old_name = data.pop('_old_name', data.get('name'))
                if not old_name:
                    return jsonify({"status": "error", "message": "Original connection name is required"}), 400
                success, message = db_manager.update_connection(old_name, data)
                if success:
                    return jsonify({"status": "success", "message": message})
                else:
                    return jsonify({"status": "error", "message": message}), 400
            except Exception as e:
                logger.exception("Failed to update connection")
                return jsonify({"status": "error", "message": "Internal server error"}), 500
        elif request.method == 'DELETE':
            try:
                data = request.get_json()
                connection_name = data.get('name')
                if not connection_name:
                    return jsonify({"status": "error", "message": "Connection name is required"}), 400
                success, message = db_manager.delete_connection(connection_name)
                if success:
                    return jsonify({"status": "success", "message": message})
                else:
                    return jsonify({"status": "error", "message": message}), 400
            except Exception as e:
                logger.exception(" Failed to delete connection")
                return jsonify({"status": "error", "message": "Internal server error"}), 500


    @app.route('/api/connections/test', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def test_connection():
        """Test a database connection, either by saved name or by raw credentials in the body."""
        try:
            data = request.get_json()
            if 'name' in data:
                success, message = db_manager.test_connection_by_name(data['name'])
            else:
                success, message = db_manager.test_connection(data)
            return jsonify({"success": success, "message": message})
        except Exception as e:
            logger.exception(" Connections Test Failed")
            return jsonify({"success": False, "message": f"Error testing connection: {str(e)}"})


    @app.route('/api/documents/upload', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def upload_documents():
        """Placeholder endpoint — always returns success without persisting anything (legacy Knowledge Base upload now handled by knowledge_bp)."""
        return jsonify({"status": "success"})


    @app.route('/api/system/stats', methods=['GET'])
    @auth.login_required
    @auth.dev_or_admin_required
    def system_stats():
        """Get system statistics including cache performance"""
        session_stats = nlq_engine.session_manager.get_session_stats()
        cache_stats = {
            "cached_chains": len(nlq_engine.chain_cache),
            "cached_databases": len(nlq_engine.db_cache)
        }
        return jsonify({"status": "success", "session_stats": session_stats, "cache_stats": cache_stats})


    @app.route('/api/clients', methods=['GET'])
    @auth.login_required
    def get_clients():
        """List all clients (any authenticated user — used by PPT/report client pickers)."""
        return jsonify(_list_clients_db())


    @app.route('/api/admin/clients', methods=['GET', 'POST'])
    @auth.login_required
    @auth.admin_required
    def api_clients():
        """Admin: list all clients (without ppt_history) or create a new client."""
        if request.method == 'GET':
            safe = [{k: v for k, v in c.items() if k != 'ppt_history'} for c in _list_clients_db()]
            return jsonify(safe)

        data = request.get_json() or {}
        client_id = (data.get('id') or '').strip()
        if not client_id:
            return jsonify({"status": "error", "message": "Client id is required"}), 400
        if _get_client_db(client_id):
            return jsonify({"status": "error", "message": "A client with this id already exists"}), 400
        client = {
            "id": client_id,
            "name": (data.get('name') or client_id).strip(),
            "url": (data.get('url') or '').strip(),
            "industry": (data.get('industry') or '').strip(),
            "competitors": [s.strip() for s in data.get('competitors', []) if s.strip()],
            "our_services_for_client": [s.strip() for s in data.get('our_services_for_client', []) if s.strip()],
            "all_our_services": [s.strip() for s in data.get('all_our_services', []) if s.strip()],
            "email_db_config": data.get('email_db_config') or {},
        }
        _save_client_db(client)
        logger.info(f"Admin created client: {client_id}")
        return jsonify({"status": "success", "client": client}), 201


    @app.route('/api/admin/clients/<client_id>', methods=['PUT'])
    @auth.login_required
    @auth.admin_required
    def api_update_client(client_id):
        """Admin: update a client's profile fields (name, url, industry, services, email_db_config)."""
        existing = _get_client_db(client_id)
        if not existing:
            return jsonify({"status": "error", "message": "Client not found"}), 404
        data = request.get_json() or {}
        existing['name'] = (data.get('name') or existing.get('name', client_id)).strip()
        existing['url'] = (data.get('url') or '').strip()
        existing['industry'] = (data.get('industry') or '').strip()
        existing['competitors'] = [s.strip() for s in data.get('competitors', existing.get('competitors', [])) if s.strip()]
        existing['our_services_for_client'] = [s.strip() for s in data.get('our_services_for_client', existing.get('our_services_for_client', [])) if s.strip()]
        existing['all_our_services'] = [s.strip() for s in data.get('all_our_services', existing.get('all_our_services', [])) if s.strip()]
        if 'email_db_config' in data:
            existing['email_db_config'] = data['email_db_config'] or {}
        _save_client_db(existing)
        logger.info(f"Admin updated client: {client_id}")
        safe = {k: v for k, v in existing.items() if k != 'ppt_history'}
        return jsonify({"status": "success", "client": safe})


    @app.route('/api/admin/clients/<client_id>', methods=['DELETE'])
    @auth.login_required
    @auth.admin_required
    def api_delete_client(client_id):
        """Admin: delete a client."""
        if not _get_client_db(client_id):
            return jsonify({"status": "error", "message": "Client not found"}), 404
        _delete_client_db(client_id)
        logger.info(f"Admin deleted client: {client_id}")
        return jsonify({"status": "success"})


    def _get_clients_dict():
        """Return all clients as {client_id: client_dict} (thin wrapper over clients_db.load_clients)."""
        return _load_clients_db()

