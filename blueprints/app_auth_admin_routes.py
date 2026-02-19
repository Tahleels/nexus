"""blueprints/app_auth_admin_routes.py — auth, admin-pages, admin-users, admin-org, admin-dev-resources routes

Relocated from app.py verbatim (Phase 3 Slice 2 route reorganization).
Route functions are nested inside register_auth_admin_routes(...) exactly
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
import config
import auth
import token_limits
import org_db
import nexus_sync_db
from logging_config import get_logger
from sentry_config import set_user_context as _sentry_set_user
import app_db as _app_db

logger = get_logger(__name__)


def register_auth_admin_routes(app):
    """Register auth + admin (users/org/dev-resources) routes onto app.

    Args:
        app: The Flask application instance.
    """

    @app.route('/login')
    def login_page():
        """Render the login page, redirecting to the dashboard if already authenticated."""
        if auth.current_user():
            return redirect(url_for('dashboard'))
        return render_template('login.html')


    @app.route('/api/auth/login', methods=['POST'])
    def login():
        """Step 1 of login: verify email/password, then email a one-time OTP code."""
        data = request.get_json() or {}
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        if not email or not password:
            return jsonify({"status": "error", "message": "Email and password are required"}), 400
        user = auth.get_user_by_email(email)
        if not user or not auth.verify_password(password, user['password_hash']):
            return jsonify({"status": "error", "message": "Invalid email or password"}), 401
        if not user['is_active']:
            return jsonify({"status": "error", "message": "Account is disabled"}), 403

        otp = auth.store_otp(user['id'])
        sent = auth.send_otp_email(user['email'], otp, user['username'])
        if not sent:
            return jsonify({"status": "error", "message": "Could not send OTP email. Check SMTP configuration."}), 500

        logger.info(f"OTP requested: {user['username']} from {request.remote_addr}")
        return jsonify({"status": "otp_required", "user_id": user['id']})


    @app.route('/api/auth/verify-otp', methods=['POST'])
    def verify_otp():
        """Step 2 of login: validate the OTP, then create a session and set the auth cookie."""
        data = request.get_json() or {}
        user_id = data.get('user_id')
        otp     = data.get('otp', '').strip()
        if not user_id or not otp:
            return jsonify({"status": "error", "message": "user_id and otp are required"}), 400

        if not auth.verify_otp(int(user_id), otp):
            return jsonify({"status": "error", "message": "Invalid or expired OTP"}), 401

        user = auth.get_user_by_id(int(user_id))
        if not user or not user.get('is_active'):
            return jsonify({"status": "error", "message": "Account not found or disabled"}), 403

        token = auth.create_session(user['id'], request.remote_addr, request.headers.get('User-Agent', ''))
        auth.update_last_login(user['id'])
        _sentry_set_user(user['id'], user['username'], user['role'])
        response = make_response(jsonify({
            "status": "success",
            "user": {"id": user['id'], "username": user['username'], "role": user['role']}
        }))
        response.set_cookie(config.SESSION_COOKIE_NAME, token, httponly=True, samesite='Lax',
                            max_age=config.PERMANENT_SESSION_HOURS * 3600)
        logger.info(f"Login: {user['username']} ({user['role']}) from {request.remote_addr}")
        return response


    @app.route('/api/auth/logout', methods=['POST'])
    def logout():
        """Invalidate the current session token and clear the auth cookie."""
        token = request.cookies.get(config.SESSION_COOKIE_NAME)
        if token:
            auth.invalidate_session(token)
        response = make_response(jsonify({"status": "success"}))
        response.delete_cookie(config.SESSION_COOKIE_NAME)
        return response


    @app.route('/api/auth/change-password', methods=['POST'])
    @auth.login_required
    def change_password():
        """Change the current user's password after verifying their current one."""
        user = auth.current_user()
        data = request.get_json() or {}
        current_password = data.get('current_password', '')
        new_password = data.get('new_password', '')
        if not current_password or not new_password:
            return jsonify({"status": "error", "message": "Both current and new password are required"}), 400
        if len(new_password) < 8:
            return jsonify({"status": "error", "message": "New password must be at least 8 characters"}), 400
        full_user = auth.get_user_by_id(user['id'])
        stored_hash = _get_password_hash(user['id'])
        if not stored_hash or not auth.verify_password(current_password, stored_hash):
            return jsonify({"status": "error", "message": "Current password is incorrect"}), 401
        ok, msg = auth.reset_password(user['id'], new_password)
        if not ok:
            return jsonify({"status": "error", "message": msg}), 500
        logger.info("Password changed for user %s", user['username'])
        return jsonify({"status": "success", "message": "Password updated"})


    def _get_password_hash(user_id: int) -> str | None:
        """Return the stored bcrypt password_hash for user_id, or None if not found."""
        with _app_db.get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT password_hash FROM users WHERE id = ?", user_id)
            row = cursor.fetchone()
            return row[0] if row else None


    @app.route('/api/auth/token-usage', methods=['GET'])
    @auth.login_required
    def token_usage():
        """Returns current user's token usage for today."""
        summary = token_limits.get_usage_summary(auth.current_user())
        return jsonify(summary)


    @app.route('/admin/users')
    @auth.login_required
    @auth.admin_required
    def admin_users():
        """Render the admin user-management page."""
        return render_template('admin_users.html')


    @app.route('/admin/assignments')
    @auth.login_required
    @auth.dev_or_admin_required
    def admin_assignments():
        """Render the admin/dev agent-assignment page."""
        return render_template('admin_assignments.html')


    @app.route('/admin/clients')
    @auth.login_required
    @auth.admin_required
    def admin_clients():
        """Render the admin client-configuration page."""
        return render_template('admin_clients.html')


    @app.route('/admin/departments')
    @auth.login_required
    @auth.admin_required
    def admin_departments():
        """Render the admin departments & projects management page."""
        return render_template('admin_departments.html')


    @app.route('/admin/nexus-mapping')
    @auth.login_required
    @auth.admin_required
    def admin_nexus_mapping():
        """Render the admin portal project mapping page."""
        return render_template('admin_nexus_mapping.html')


    @app.route('/admin/token-quotas')
    @auth.login_required
    @auth.admin_required
    def admin_token_quotas():
        """Render the admin token-quota management page."""
        return render_template('admin_token_quotas.html')


    @app.route('/api/admin/users', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_list_users():
        """Return all users (password hashes stripped, dates stringified)."""
        users = auth.get_all_users()
        safe = [{k: v for k, v in u.items() if k != 'password_hash'} for u in users]
        for u in safe:
            for field in ('created_at', 'last_login'):
                if u.get(field):
                    u[field] = str(u[field])
        return jsonify(safe)


    @app.route('/api/admin/users', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_create_user():
        """Create a user and auto-sync their org assignments from the portal if possible."""
        data = request.get_json() or {}
        for field in ['username', 'email', 'password', 'role']:
            if not data.get(field):
                return jsonify({"status": "error", "message": f"Missing field: {field}"}), 400
        if data['role'] not in {'admin', 'dev', 'user'}:
            return jsonify({"status": "error", "message": "Role must be admin, dev, or user"}), 400
        creator = auth.current_user()
        success, message, new_user_id = auth.create_user(
            data['username'], data['email'], data['password'], data['role'], creator['id'])
        if success and new_user_id:
            try:
                nexus_sync_db.sync_user_by_email(new_user_id, data['email'], assigned_by=creator['id'])
            except Exception as _se:
                logger.warning("nexus_sync on user create failed: %s", _se)
        return jsonify({"status": "success" if success else "error", "message": message}), 201 if success else 400


    @app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
    @auth.login_required
    @auth.admin_required
    def api_update_user(user_id):
        """Update a user's email, role, or active status."""
        data = request.get_json() or {}
        success, message = auth.update_user(user_id, data)
        return jsonify({"status": "success" if success else "error", "message": message})


    @app.route('/api/admin/users/<int:user_id>/reset-password', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_reset_password(user_id):
        """Admin-initiated password reset for another user."""
        data = request.get_json() or {}
        new_pw = data.get('new_password', '')
        if len(new_pw) < 8:
            return jsonify({"status": "error", "message": "Password must be at least 8 characters"}), 400
        success, message = auth.reset_password(user_id, new_pw)
        return jsonify({"status": "success" if success else "error", "message": message})


    @app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
    @auth.login_required
    @auth.admin_required
    def api_delete_user(user_id):
        """Delete a user (and cascade-clean their sessions/data); admins cannot delete themselves."""
        if auth.current_user()['id'] == user_id:
            return jsonify({"status": "error", "message": "Cannot delete your own account"}), 400
        auth.invalidate_all_user_sessions(user_id)
        success, message = auth.delete_user(user_id)
        return jsonify({"status": "success" if success else "error", "message": message})


    @app.route('/api/admin/token-quotas', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_list_token_quotas():
        """Return every user's today's token usage plus their effective quota."""
        rows = token_limits.get_all_users_usage_today()
        for row in rows:
            for field in ('created_at', 'last_login'):
                if row.get(field):
                    row[field] = str(row[field])
        return jsonify(rows)


    @app.route('/api/admin/token-quotas/<int:user_id>', methods=['PUT'])
    @auth.login_required
    @auth.admin_required
    def api_set_token_quota(user_id):
        """Set (or clear, via null) a custom daily token quota for a user."""
        data = request.get_json() or {}
        quota = data.get('token_quota')
        if quota is not None:
            try:
                quota = int(quota)
                if quota < 1000:
                    return jsonify({"status": "error", "message": "Quota must be at least 1,000 tokens"}), 400
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "Invalid quota value"}), 400
        success, message = auth.set_user_token_quota(user_id, quota)
        return jsonify({"status": "success" if success else "error", "message": message})


    @app.route('/api/admin/users/<int:user_id>/org', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_get_user_org(user_id):
        """Return a user's department/project ids and roles."""
        return jsonify(org_db.get_user_org_assignments(user_id))


    @app.route('/api/admin/users/<int:user_id>/org', methods=['PUT'])
    @auth.login_required
    @auth.admin_required
    def api_set_user_org(user_id):
        """Replace a user's department and/or project assignments."""
        data = request.get_json() or {}
        me = auth.current_user()
        # Accept both old format (dept_ids list) and new format (dept_assignments [{id,role}])
        if 'dept_assignments' in data:
            ok, msg = org_db.set_user_departments(user_id, data['dept_assignments'], me['id'])
            if not ok:
                return jsonify({"status": "error", "message": msg}), 400
        elif 'dept_ids' in data:
            ok, msg = org_db.set_user_departments(user_id, data['dept_ids'], me['id'])
            if not ok:
                return jsonify({"status": "error", "message": msg}), 400
        if 'project_assignments' in data:
            ok, msg = org_db.set_user_projects(user_id, data['project_assignments'], me['id'])
            if not ok:
                return jsonify({"status": "error", "message": msg}), 400
        elif 'project_ids' in data:
            ok, msg = org_db.set_user_projects(user_id, data['project_ids'], me['id'])
            if not ok:
                return jsonify({"status": "error", "message": msg}), 400
        return jsonify({"status": "success"})


    @app.route('/api/admin/users/org-assignments', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_all_user_org_assignments():
        """Return every user with their department/project assignment counts (admin table)."""
        return jsonify(org_db.get_all_user_org_assignments())


    @app.route('/api/admin/agent-assignments', methods=['GET'])
    @auth.login_required
    @auth.dev_or_admin_required
    def api_get_assignments():
        """List all user-to-agent assignments (legacy single-agent assignment table)."""
        assignments = auth.get_all_assignments()
        return jsonify(assignments)


    @app.route('/api/admin/agent-assignments/<int:user_id>', methods=['POST'])
    @auth.login_required
    @auth.dev_or_admin_required
    def api_set_user_agents(user_id):
        """Replace the set of agents assigned to a user."""
        data = request.get_json() or {}
        agent_names = data.get('agent_names', [])
        assigner = auth.current_user()
        success, message = auth.set_user_agents(user_id, agent_names, assigner['id'])
        return jsonify({'status': 'success' if success else 'error', 'message': message})


    @app.route('/api/admin/token-usage/breakdown', methods=['GET'])
    @auth.login_required
    @auth.dev_or_admin_required
    def admin_token_usage_breakdown():
        """
        Admin/dev view: today's token usage grouped by call_type across all users.
        Useful for spotting which feature (chat vs dashboard vs analysis) consumes the most.
        """
        try:
            with auth._get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT u.username,
                           r.name         AS role,
                           t.call_type,
                           SUM(t.tokens_used) AS tokens,
                           COUNT(*)           AS requests
                    FROM  token_usage t
                    JOIN  users u ON u.id = t.user_id
                    JOIN  roles r ON r.id = u.role_id
                    WHERE t.used_at >= CAST(GETUTCDATE() AS DATE)
                    GROUP BY u.username, r.name, t.call_type
                    ORDER BY tokens DESC
                """)
                cols = [d[0] for d in cursor.description]
                rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
            return jsonify({"status": "success", "data": rows})
        except Exception as e:
            logger.exception("admin_token_usage_breakdown failed")
            return jsonify({"status": "error", "message": str(e)}), 500


    @app.route('/api/admin/departments', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_list_departments():
        """List all departments with member counts."""
        return jsonify(org_db.get_all_departments())


    @app.route('/api/admin/departments', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_create_department():
        """Create a new department."""
        data = request.get_json() or {}
        if not data.get('name', '').strip():
            return jsonify({"status": "error", "message": "Name is required"}), 400
        me = auth.current_user()
        ok, msg, dept_id = org_db.create_department(
            data['name'], data.get('description', ''), data.get('color', '#6366f1'), me['id'])
        if ok:
            return jsonify({"status": "success", "id": dept_id, "message": msg}), 201
        return jsonify({"status": "error", "message": msg}), 400


    @app.route('/api/admin/departments/<int:dept_id>', methods=['PUT'])
    @auth.login_required
    @auth.admin_required
    def api_update_department(dept_id):
        """Update a department's name/description/color."""
        data = request.get_json() or {}
        ok, msg = org_db.update_department(dept_id, data)
        return jsonify({"status": "success" if ok else "error", "message": msg})


    @app.route('/api/admin/departments/<int:dept_id>', methods=['DELETE'])
    @auth.login_required
    @auth.admin_required
    def api_delete_department(dept_id):
        """Delete a department and clear it from all users/resources that reference it."""
        ok, msg = org_db.delete_department(dept_id)
        return jsonify({"status": "success" if ok else "error", "message": msg})


    @app.route('/api/admin/nexus/projects', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_list_projects():
        """Return all portal projects via the cross-database view (always live)."""
        return jsonify(nexus_sync_db.get_portal_projects())


    @app.route('/api/admin/nexus/status', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_status():
        """Check whether the cross-database views are reachable right now."""
        return jsonify(nexus_sync_db.view_status())


    @app.route('/api/admin/nexus/mappings', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_get_mappings():
        """Return all portal project → local dept/project mappings."""
        return jsonify(nexus_sync_db.get_all_mappings())


    @app.route('/api/admin/nexus/mappings', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_upsert_mapping():
        """Create or update a mapping: nexus_project_id → dept_id and/or project_id."""
        data = request.get_json() or {}
        if not data.get('nexus_project_id'):
            return jsonify({"status": "error", "message": "nexus_project_id is required"}), 400
        me = auth.current_user()
        ok, msg = nexus_sync_db.upsert_mapping(
            int(data['nexus_project_id']),
            data.get('nexus_project_name', ''),
            data.get('dept_id') or None,
            data.get('project_id') or None,
            me['id'],
        )
        return jsonify({"status": "success" if ok else "error", "message": msg}), 200 if ok else 400


    @app.route('/api/admin/nexus/mappings/<int:nexus_project_id>', methods=['DELETE'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_delete_mapping(nexus_project_id):
        """Remove a portal project → local dept/project mapping."""
        ok, msg = nexus_sync_db.delete_mapping(nexus_project_id)
        return jsonify({"status": "success" if ok else "error", "message": msg})


    @app.route('/api/admin/nexus/sync-user/<int:user_id>', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_sync_user(user_id):
        """Re-apply org assignments for one user from the local mirror."""
        users = auth.get_all_users()
        user = next((u for u in users if u['id'] == user_id), None)
        if not user:
            return jsonify({"status": "error", "message": "User not found"}), 404
        me = auth.current_user()
        result = nexus_sync_db.sync_user_by_email(user_id, user['email'], assigned_by=me['id'])
        return jsonify({"status": "success", **result})


    @app.route('/api/admin/nexus/sync-all', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_sync_all():
        """Re-apply org assignments for all local users from the local mirror."""
        me = auth.current_user()
        users = auth.get_all_users()
        results = []
        for u in users:
            r = nexus_sync_db.sync_user_by_email(u['id'], u['email'], assigned_by=me['id'])
            results.append({"user_id": u['id'], "email": u['email'], **r})
        synced = sum(1 for r in results if r['synced'])
        return jsonify({"status": "success", "total": len(results), "synced": synced, "details": results})


    @app.route('/api/admin/nexus/sync-records', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_nexus_sync_records():
        """Return per-user org-assignment sync records."""
        return jsonify(nexus_sync_db.get_user_sync_records())


    @app.route('/api/admin/projects', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_list_projects():
        """List all projects with member counts."""
        return jsonify(org_db.get_all_projects())


    @app.route('/api/admin/projects', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_create_project():
        """Create a new project."""
        data = request.get_json() or {}
        if not data.get('name', '').strip():
            return jsonify({"status": "error", "message": "Name is required"}), 400
        me = auth.current_user()
        ok, msg, project_id = org_db.create_project(
            data['name'], data.get('description', ''),
            data.get('color', '#0ea5e9'), me['id'])
        if ok:
            return jsonify({"status": "success", "id": project_id, "message": msg}), 201
        return jsonify({"status": "error", "message": msg}), 400


    @app.route('/api/admin/projects/<int:project_id>', methods=['PUT'])
    @auth.login_required
    @auth.admin_required
    def api_update_project(project_id):
        """Update a project's name/description/color."""
        data = request.get_json() or {}
        ok, msg = org_db.update_project(project_id, data)
        return jsonify({"status": "success" if ok else "error", "message": msg})


    @app.route('/api/admin/projects/<int:project_id>', methods=['DELETE'])
    @auth.login_required
    @auth.admin_required
    def api_delete_project(project_id):
        """Delete a project and clear it from all users/resources that reference it."""
        ok, msg = org_db.delete_project(project_id)
        return jsonify({"status": "success" if ok else "error", "message": msg})


    @app.route('/api/admin/resources/<table>/org', methods=['GET'])
    @auth.login_required
    @auth.dev_or_admin_required
    def api_get_resource_org(table):
        """List a resource table's rows enriched with their dept/project assignments."""
        rows = org_db.get_resources_org(table)
        return jsonify(rows)


    @app.route('/api/admin/resources/<table>/<resource_id>/org', methods=['PUT'])
    @auth.login_required
    @auth.dev_or_admin_required
    def api_set_resource_org(table, resource_id):
        """Replace the dept/project assignments for one resource (agent/job/workflow)."""
        data = request.get_json() or {}
        # Accept arrays (new UI) or single values (backward compat)
        dept_ids    = data.get('dept_ids')
        project_ids = data.get('project_ids')
        if dept_ids is None:
            d = data.get('department_id')
            dept_ids = [d] if d else []
        if project_ids is None:
            p = data.get('project_id')
            project_ids = [p] if p else []
        ok, msg = org_db.set_resource_org(table, resource_id, dept_ids, project_ids)
        return jsonify({"status": "success" if ok else "error", "message": msg})


    @app.route('/api/org/context', methods=['GET'])
    @auth.login_required
    def api_org_context():
        """Return the departments/projects visible to the current user (sidebar context selector)."""
        user = auth.current_user()
        ctx = org_db.get_org_context_for_user(user['id'], user['role'])
        return jsonify(ctx)


    @app.route('/api/admin/dev-resources', methods=['GET'])
    @auth.login_required
    @auth.admin_required
    def api_get_dev_resources():
        """List all dev resource assignments, optionally filtered by dev_id."""
        from app_db import get_all_dev_resource_assignments
        dev_id = request.args.get('dev_id', type=int)
        rows = get_all_dev_resource_assignments(dev_user_id=dev_id)
        return jsonify(rows)


    @app.route('/api/admin/dev-resources', methods=['POST'])
    @auth.login_required
    @auth.admin_required
    def api_assign_dev_resource():
        """Assign a resource (db_connection, watch_dir, sharepoint_watch) to a dev user."""
        from app_db import assign_resource_to_dev
        data = request.get_json() or {}
        dev_user_id   = data.get('dev_user_id')
        resource_type = data.get('resource_type')
        resource_id   = data.get('resource_id')
        if not all([dev_user_id, resource_type, resource_id]):
            return jsonify({"status": "error", "message": "dev_user_id, resource_type and resource_id are required"}), 400
        if resource_type not in ('db_connection', 'watch_dir', 'sharepoint_watch'):
            return jsonify({"status": "error", "message": "Invalid resource_type"}), 400
        admin = auth.current_user()
        success, message = assign_resource_to_dev(int(dev_user_id), resource_type, int(resource_id), admin['id'])
        return jsonify({"status": "success" if success else "error", "message": message}), (200 if success else 500)


    @app.route('/api/admin/dev-resources', methods=['DELETE'])
    @auth.login_required
    @auth.admin_required
    def api_unassign_dev_resource():
        """Remove a resource assignment from a dev user."""
        from app_db import unassign_resource_from_dev
        data = request.get_json() or {}
        dev_user_id   = data.get('dev_user_id')
        resource_type = data.get('resource_type')
        resource_id   = data.get('resource_id')
        if not all([dev_user_id, resource_type, resource_id]):
            return jsonify({"status": "error", "message": "dev_user_id, resource_type and resource_id are required"}), 400
        success, message = unassign_resource_from_dev(int(dev_user_id), resource_type, int(resource_id))
        return jsonify({"status": "success" if success else "error", "message": message}), (200 if success else 500)

