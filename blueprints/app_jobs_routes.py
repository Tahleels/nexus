"""
app_jobs_routes.py — Nexus AI · Jobs & Scheduler  (v3)

Flask blueprint exposing the HTTP surface for scheduled jobs: CRUD, manual
run/toggle actions, and execution logs (including the full chat-style output
panel used by the job monitor UI).

Routes
------
  GET  /api/jobs                                       — list jobs visible to the current user
  POST /api/jobs                                        — create a job (dev/admin)
  GET  /api/jobs/<job_id>                                — get one job (owner or admin)
  PUT  /api/jobs/<job_id>                                — update a job (dev/admin, owner-checked)
  DELETE /api/jobs/<job_id>                              — delete a job (dev/admin, owner-checked)

  POST /api/jobs/<job_id>/run                            — run a job immediately (dev/admin)
  POST /api/jobs/<job_id>/toggle                         — enable/disable a job (dev/admin)

  GET  /api/jobs/logs                                    — recent executions across all visible jobs
  GET  /api/jobs/logs/<job_id>                           — recent executions for one job
  GET  /api/jobs/logs/<job_id>/<exec_id>/output           — full query_result (sql, columns, data,
                                                             insights, analysis) for the monitor's
                                                             chat-style output panel

  GET  /jobs                                             — Jobs management page (registered via
                                                             `register_jobs_routes`)
  GET  /job-monitor                                       — Job monitor page (registered via
                                                             `register_jobs_routes`)
"""

from flask import Blueprint, jsonify, request
import auth
import execution_logger as el
from job_manager       import job_manager
from scheduler_service import scheduler_service
import org_db
from logging_config import get_logger

logger = get_logger(__name__)

jobs_bp = Blueprint("jobs", __name__)


def _me():
    """Return ``(user_id, role)`` for the current session, or ``(0, "user")`` if logged out."""
    u = auth.current_user()
    if not u:
        return 0, "user"
    return u["id"], u["role"]


def _get_jobs_for_user_with_org(user_id: int, role: str) -> list:
    """Return all jobs visible to the user, enriched with org data.

    Admins see every job.
    Non-admins see jobs that share at least one of their departments/projects.
    Unscoped jobs (no dept/project assigned) are only visible to their creator.
    """
    jobs = job_manager.get_all_jobs()

    for job in jobs:
        live = scheduler_service.get_next_run(job["id"])
        if live:
            job["next_run"] = live
        job["is_running"] = scheduler_service.is_running(job["id"])

    # Enrich with org context
    try:
        res_org = org_db.get_resources_org('app_jobs')
        org_map = {r['id']: r for r in res_org}
        for j in jobs:
            org_row = org_map.get(str(j.get('id'))) or {}
            j['dept_ids']      = org_row.get('dept_ids', [])
            j['project_ids']   = org_row.get('project_ids', [])
            j['dept_names']    = org_row.get('dept_names', [])
            j['dept_colors']   = org_row.get('dept_colors', [])
            j['project_names'] = org_row.get('project_names', [])
            j['department_id'] = org_row.get('department_id')
            j['project_id']    = org_row.get('project_id')
            j['dept_name']     = org_row.get('dept_name')
            j['project_name']  = org_row.get('project_name')
            dept_colors        = org_row.get('dept_colors') or []
            j['dept_color']    = dept_colors[0] if dept_colors else None
    except Exception:
        logger.exception("Failed to enrich jobs with org data")

    if role == 'admin':
        return jobs

    # Non-admins: filter by their org memberships
    try:
        user_org      = org_db.get_user_org_assignments(user_id)
        user_dept_ids = set(user_org.get('dept_ids', []))
        user_proj_ids = set(user_org.get('project_ids', []))

        def _visible(j):
            j_depts = set(j.get('dept_ids') or [])
            j_projs = set(j.get('project_ids') or [])
            if j_depts or j_projs:
                # Scoped job: visible when user shares at least one org
                return bool(j_depts & user_dept_ids) or bool(j_projs & user_proj_ids)
            # Unscoped job: only the creator sees it
            return j.get('created_by') == user_id

        return [j for j in jobs if _visible(j)]
    except Exception:
        return [j for j in jobs if j.get('created_by') == user_id]


# ── CRUD ──────────────────────────────────────────────────────────────────────

@jobs_bp.route("/api/jobs", methods=["GET"])
@auth.login_required
def api_list_jobs():
    """List all jobs visible to the current user, enriched with org/live-status data."""
    user_id, role = _me()
    jobs = _get_jobs_for_user_with_org(user_id, role)
    return jsonify(jobs)


@jobs_bp.route("/api/jobs", methods=["POST"])
@auth.login_required
@auth.dev_or_admin_required
def api_create_job():
    """Create a new job. Requires ``name``, ``agent_name``, ``nlq_prompt`` in the JSON body.

    Schedules the job immediately (via `scheduler_service.on_job_created`) if
    it's created enabled.
    """
    data = request.get_json() or {}
    for f in ["name", "agent_name", "nlq_prompt"]:
        if not data.get(f):
            return jsonify({"status": "error", "message": f"Missing field: {f}"}), 400
    user    = auth.current_user()
    uid     = user["id"]       if user else 0
    uname   = user["username"] if user else "system"
    ok, msg, job = job_manager.create_job(data, created_by=uid,
                                           created_by_username=uname)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    scheduler_service.on_job_created(job)
    # Auto-assign org context if provided (e.g. user created job while dept/project is selected)
    dept_ids    = data.get('dept_ids') or ([data['department_id']] if data.get('department_id') else [])
    project_ids = data.get('project_ids') or ([data['project_id']] if data.get('project_id') else [])
    if dept_ids or project_ids:
        try:
            org_db.set_resource_org('app_jobs', job['id'], dept_ids, project_ids)
        except Exception:
            pass
    return jsonify({"status": "success", "message": msg, "job": job}), 201


@jobs_bp.route("/api/jobs/<job_id>", methods=["GET"])
@auth.login_required
def api_get_job(job_id):
    """Get one job by id (owner or admin only), with live ``next_run``/``is_running`` status."""
    user_id, role = _me()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found"}), 404
    if role != "admin" and job.get("created_by") != user_id:
        return jsonify({"status": "error", "message": "Permission denied"}), 403
    job["next_run"]   = scheduler_service.get_next_run(job_id)
    job["is_running"] = scheduler_service.is_running(job_id)
    return jsonify(job)


@jobs_bp.route("/api/jobs/<job_id>", methods=["PUT"])
@auth.login_required
@auth.dev_or_admin_required
def api_update_job(job_id):
    """Update editable fields of a job (owner or admin) and reschedule it accordingly."""
    user_id, role = _me()
    data = request.get_json() or {}
    ok, msg = job_manager.update_job(job_id, data,
                                      requesting_user_id=user_id,
                                      requesting_role=role)
    if not ok:
        return jsonify({"status": "error", "message": msg}), (403 if "Permission" in msg else 400)
    job = job_manager.get_job(job_id)
    scheduler_service.on_job_updated(job)
    return jsonify({"status": "success", "message": msg, "job": job})


@jobs_bp.route("/api/jobs/<job_id>", methods=["DELETE"])
@auth.login_required
@auth.dev_or_admin_required
def api_delete_job(job_id):
    """Delete a job (owner or admin). Unschedules it before deleting the DB row."""
    user_id, role = _me()
    scheduler_service.on_job_deleted(job_id)
    ok, msg = job_manager.delete_job(job_id,
                                      requesting_user_id=user_id,
                                      requesting_role=role)
    if not ok:
        return jsonify({"status": "error", "message": msg}), (403 if "Permission" in msg else 404)
    return jsonify({"status": "success", "message": msg})


# ── Actions ───────────────────────────────────────────────────────────────────

@jobs_bp.route("/api/jobs/<job_id>/run", methods=["POST"])
@auth.login_required
@auth.dev_or_admin_required
def api_run_job(job_id):
    """Trigger an immediate "Run now" of a job (owner or admin) via `scheduler_service.run_now`."""
    user_id, role = _me()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found"}), 404
    if role != "admin" and job.get("created_by") != user_id:
        return jsonify({"status": "error", "message": "Permission denied"}), 403
    ok, msg = scheduler_service.run_now(job_id)
    return jsonify({"status": "success" if ok else "error", "message": msg})


@jobs_bp.route("/api/jobs/<job_id>/toggle", methods=["POST"])
@auth.login_required
@auth.dev_or_admin_required
def api_toggle_job(job_id):
    """Flip a job's enabled/disabled state (owner or admin) and (un)schedule it accordingly."""
    user_id, role = _me()
    ok, msg, new_state = job_manager.toggle_job(job_id,
                                                  requesting_user_id=user_id,
                                                  requesting_role=role)
    if not ok:
        return jsonify({"status": "error", "message": msg}), (403 if "Permission" in msg else 404)
    scheduler_service.on_job_toggled(job_id, new_state)
    return jsonify({"status": "success", "message": msg, "enabled": new_state})


# ── Logs ──────────────────────────────────────────────────────────────────────

@jobs_bp.route("/api/jobs/logs", methods=["GET"])
@auth.login_required
def api_all_logs():
    """Return recent executions (no query_result payload) across every job the user can see."""
    user_id, role = _me()
    jobs  = _get_jobs_for_user_with_org(user_id, role)
    limit = int(request.args.get("limit", 50))
    return jsonify(el.get_all_recent_logs([j["id"] for j in jobs], limit=limit))


@jobs_bp.route("/api/jobs/logs/<job_id>", methods=["GET"])
@auth.login_required
def api_job_logs(job_id):
    """Return recent executions (no query_result payload) for one job (owner or admin)."""
    user_id, role = _me()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify([])
    if role != "admin" and job.get("created_by") != user_id:
        return jsonify({"status": "error", "message": "Permission denied"}), 403
    limit = int(request.args.get("limit", 20))
    return jsonify(el.get_logs(job_id, limit=limit))


@jobs_bp.route("/api/jobs/logs/<job_id>/<exec_id>/output", methods=["GET"])
@auth.login_required
def api_execution_output(job_id, exec_id):
    """
    Returns the full query_result for one execution:
      { sql_query, columns, data, row_count, insights, analysis, truncated }
    Used by the monitor Output panel.
    """
    user_id, role = _me()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Job not found"}), 404
    if role != "admin" and job.get("created_by") != user_id:
        return jsonify({"status": "error", "message": "Permission denied"}), 403

    entry = el.get_execution(job_id, exec_id)
    if not entry:
        return jsonify({"status": "error", "message": "Execution not found"}), 404

    qr = entry.get("query_result")
    if not qr:
        return jsonify({"status": "error", "message": "No output stored for this execution"}), 404

    return jsonify({"status": "success", "query_result": qr,
                    "job_name": entry.get("job_name", ""),
                    "started_at": entry.get("started_at"),
                    "tools_completed": entry.get("tools_completed", [])})


# ── Page routes ───────────────────────────────────────────────────────────────

def register_jobs_routes(app):
    """Register `jobs_bp` on ``app`` and add the ``/jobs`` and ``/job-monitor`` page routes.

    Called once from `app.py` during application setup.

    Args:
        app: The Flask application instance.
    """
    from flask import render_template

    app.register_blueprint(jobs_bp)

    @app.route("/jobs")
    @auth.login_required
    def jobs_page():
        return render_template("jobs.html")

    @app.route("/job-monitor")
    @auth.login_required
    def job_monitor():
        return render_template("job_monitor.html")