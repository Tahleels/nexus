"""Scheduled tool-job routes for agents_hub_bp, plus the background
APScheduler instance that drives them (moved here verbatim from its
original module-level location in agents_hub_bp.py, since nothing
outside this route group ever touches it). Split out in Phase 3 Slice 4.
"""

import sys, os, uuid, json, logging, threading, re, calendar
from datetime import datetime, timedelta, date
from flask import (render_template, jsonify, request,
                   Response, stream_with_context, redirect, url_for, abort)
import auth
import token_limits
from agents_hub_bp import agents_hub_bp
from agents_hub_bp import (
    logger, _ss_exec, _fix_row, _fix_rows, _is_dev_or_admin, _is_admin,
    _exec_custom_tool,
)


# ── Tool-job APScheduler (background, daemon — starts with app) ───────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _tool_job_scheduler = BackgroundScheduler(timezone='UTC', daemon=True)
    _tool_job_scheduler.start()
    _SCHEDULER_AVAILABLE = True
except ImportError:
    _SCHEDULER_AVAILABLE = False
    logger.warning("[tool-jobs] APScheduler not installed — scheduled tool jobs disabled. "
                   "Run: pip install apscheduler")

_scheduler_loaded = False


def _tool_job_dict(row: dict) -> dict:
    """Convert a raw hub_jobs (tool job) row into an API-ready dict (parses JSON columns)."""
    if not row:
        return None
    d = _fix_row(dict(row))
    d["tool_params"]   = json.loads(d.pop("tool_params_json", "{}") or "{}")
    d["tool_env_vars"] = json.loads(d.pop("tool_env_vars_json", "{}") or "{}")
    return d


def _run_tool_job(jid: str):
    """Execute a tool job — called by APScheduler or manual run thread."""
    try:
        row = _ss_exec("SELECT * FROM hub_jobs WHERE id=?", (jid,), fetchone=True)
        if not row or row.get("status") == "paused":
            return

        _ss_exec(
            "UPDATE hub_jobs SET last_run_status='running', last_run_at=GETUTCDATE() WHERE id=?",
            (jid,))

        tool_name = row.get("tool_name") or ""
        params    = json.loads(row.get("tool_params_json") or "{}")
        env_vars  = json.loads(row.get("tool_env_vars_json") or "{}")
        hub_ctx   = {"agent_env_vars": env_vars}

        # Try custom tool first (has code + venv)
        custom_row = _ss_exec(
            "SELECT * FROM hub_custom_tools WHERE name=?", (tool_name,), fetchone=True)
        if custom_row:
            result = _exec_custom_tool(_fix_row(custom_row), params, hub_ctx)
        else:
            # Built-in registry tool — inject env vars temporarily
            from core.tools.registry import execute_tool
            old_env = {}
            for k, v in env_vars.items():
                old_env[k] = os.environ.get(k)
                os.environ[k] = str(v)
            try:
                result = execute_tool(tool_name, params)
            finally:
                for k, old_v in old_env.items():
                    if old_v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = old_v

        status = "success" if result.get("success") else "failed"
        out_data = result.get("result") if result.get("success") else result.get("error", "")
        try:
            output = json.dumps(out_data, ensure_ascii=False)[:4000]
        except Exception:
            output = str(out_data)[:4000]

        _ss_exec("""
            UPDATE hub_jobs
            SET last_run_status=?, last_run_output=?,
                run_count=COALESCE(run_count,0)+1, last_run_at=GETUTCDATE()
            WHERE id=?
        """, (status, output, jid))
        logger.info(f"[tool-jobs] Job {jid} ({tool_name}) finished: {status}")

    except Exception as exc:
        logger.error(f"[tool-jobs] Job {jid} error: {exc}")
        try:
            _ss_exec(
                "UPDATE hub_jobs SET last_run_status='failed', last_run_output=? WHERE id=?",
                (str(exc)[:2000], jid))
        except Exception:
            pass


def _schedule_tool_job(row: dict):
    """Add or replace a job in APScheduler. Removes the job if status is paused."""
    if not _SCHEDULER_AVAILABLE:
        return
    jid      = row["id"]
    cron_str = (row.get("schedule") or "0 9 * * *").strip()
    try:
        _tool_job_scheduler.remove_job(jid)
    except Exception:
        pass
    if row.get("status") == "active":
        parts = cron_str.split()
        if len(parts) == 5:
            try:
                _tool_job_scheduler.add_job(
                    _run_tool_job,
                    CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                                month=parts[3], day_of_week=parts[4], timezone="UTC"),
                    id=jid, args=[jid], replace_existing=True,
                    misfire_grace_time=300, max_instances=1)
                logger.info(f"[tool-jobs] Scheduled job {jid} with cron '{cron_str}'")
            except Exception as e:
                logger.warning(f"[tool-jobs] Failed to schedule job {jid}: {e}")


def _load_scheduled_tool_jobs():
    """Load all active tool jobs into APScheduler on first request."""
    global _scheduler_loaded
    if _scheduler_loaded:
        return
    rows = _ss_exec(
        "SELECT * FROM hub_jobs WHERE job_type='tool' AND status='active'",
        fetchall=True) or []
    for row in rows:
        try:
            _schedule_tool_job(row)
        except Exception as e:
            logger.warning(f"[tool-jobs] Startup schedule error for {row.get('id')}: {e}")
    _scheduler_loaded = True
    logger.info(f"[tool-jobs] Loaded {len(rows)} scheduled tool jobs")


@agents_hub_bp.route('/api/agenthub/tool-jobs', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def list_tool_jobs():
    _load_scheduled_tool_jobs()
    rows = _ss_exec(
        "SELECT * FROM hub_jobs WHERE job_type='tool' ORDER BY created_at DESC",
        fetchall=True) or []
    return jsonify([_tool_job_dict(r) for r in rows])


@agents_hub_bp.route('/api/agenthub/tool-jobs', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def create_tool_job():
    d   = request.json or {}
    jid = str(uuid.uuid4())
    _ss_exec("""
        INSERT INTO hub_jobs
            (id, name, description, job_type, target_id, schedule,
             tool_name, tool_params_json, tool_env_vars_json, status)
        VALUES (?, ?, ?, 'tool', '', ?, ?, ?, ?, 'active')
    """, (jid,
          d.get("name") or "New Tool Job",
          d.get("description", ""),
          d.get("schedule", "0 9 * * *"),
          d.get("tool_name", ""),
          json.dumps(d.get("tool_params") or {}),
          json.dumps(d.get("tool_env_vars") or {})))

    row = _ss_exec("SELECT * FROM hub_jobs WHERE id=?", (jid,), fetchone=True)
    _schedule_tool_job(row)
    return jsonify(_tool_job_dict(row)), 201


@agents_hub_bp.route('/api/agenthub/tool-jobs/<jid>', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def get_tool_job(jid):
    row = _ss_exec("SELECT * FROM hub_jobs WHERE id=? AND job_type='tool'",
                   (jid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_tool_job_dict(row))


@agents_hub_bp.route('/api/agenthub/tool-jobs/<jid>', methods=['PUT'])
@auth.login_required
@auth.dev_or_admin_required
def update_tool_job(jid):
    d   = request.json or {}
    row = _ss_exec("SELECT * FROM hub_jobs WHERE id=? AND job_type='tool'",
                   (jid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404

    _ss_exec("""
        UPDATE hub_jobs SET
            name=?, description=?, schedule=?,
            tool_name=?, tool_params_json=?, tool_env_vars_json=?
        WHERE id=?
    """, (d.get("name", row["name"]),
          d.get("description", row.get("description", "")),
          d.get("schedule", row["schedule"]),
          d.get("tool_name", row.get("tool_name", "")),
          json.dumps(d.get("tool_params") or {}),
          json.dumps(d.get("tool_env_vars") or {}),
          jid))

    row = _ss_exec("SELECT * FROM hub_jobs WHERE id=?", (jid,), fetchone=True)
    _schedule_tool_job(row)
    return jsonify(_tool_job_dict(row))


@agents_hub_bp.route('/api/agenthub/tool-jobs/<jid>', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def delete_tool_job(jid):
    if _SCHEDULER_AVAILABLE:
        try:
            _tool_job_scheduler.remove_job(jid)
        except Exception:
            pass
    _ss_exec("DELETE FROM hub_jobs WHERE id=? AND job_type='tool'", (jid,))
    return jsonify({"ok": True})


@agents_hub_bp.route('/api/agenthub/tool-jobs/<jid>/toggle', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def toggle_tool_job(jid):
    row = _ss_exec("SELECT * FROM hub_jobs WHERE id=? AND job_type='tool'",
                   (jid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_status = "paused" if row["status"] == "active" else "active"
    _ss_exec("UPDATE hub_jobs SET status=? WHERE id=?", (new_status, jid))
    row = _ss_exec("SELECT * FROM hub_jobs WHERE id=?", (jid,), fetchone=True)
    _schedule_tool_job(row)
    return jsonify(_tool_job_dict(row))


@agents_hub_bp.route('/api/agenthub/tool-jobs/<jid>/run', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def run_tool_job_now(jid):
    row = _ss_exec("SELECT id FROM hub_jobs WHERE id=? AND job_type='tool'",
                   (jid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    t = threading.Thread(target=_run_tool_job, args=[jid], daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Job started — refresh in a moment to see results"})


@agents_hub_bp.route('/api/agenthub/tool-jobs/<jid>/status', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def tool_job_status(jid):
    row = _ss_exec(
        "SELECT last_run_status, last_run_output, last_run_at, run_count "
        "FROM hub_jobs WHERE id=?", (jid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_fix_row(row))
