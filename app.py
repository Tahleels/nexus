# app.py - CLEANED VERSION + AUTH
"""app.py — Flask application entry point and route registry for the Nexus AI Portal.

This is the process that ``python app.py`` (or a WSGI server) runs. At
import time it, in order:

  1. Adds core/, database/, services/, generators/, nlq/, blueprints/ to
     ``sys.path`` so every submodule can be imported by its flat module
     name (e.g. ``import auth`` instead of ``from core import auth``).
  2. Loads .env (``load_dotenv()``) before any module that reads
     ``os.getenv`` (config.py, auth.py, etc.) is imported.
  3. Constructs the single shared ``NLQEngine`` instance used by every BI
     chat/dashboard/report/infographic/presentation route.
  4. Registers all blueprints: jobs routes (``register_jobs_routes``),
     ``workspace_bp``, ``training_bp``, ``agents_hub_bp``, ``knowledge_bp``.
  5. Ensures the app-internal SQL Server schema exists and is migrated
     (``app_db.ensure_tables()``, ``app_db.migrate_from_json()``,
     ``app_db.ensure_portal_views()``, ``workspace_db.ensure_schema()``).
  6. Initialises feature-segmented logging (``logging_config.setup_logging``)
     before any background service starts, so startup is captured in the logs.
  7. Starts background services as best-effort, non-fatal threads: the
     filesystem document watcher and the SharePoint document watcher (each
     wrapped in try/except so a missing dependency or bad config only logs a
     warning instead of crashing the app).
  8. Initialises Sentry error tracking and, if ``TEAMS_WEBHOOK_URL`` is set,
     attaches a ``TeamsAlertHandler`` to the root logger.

Request lifecycle: a ``before_request`` hook (``load_user``) populates
``flask.g.current_user`` via ``auth.load_current_user()`` and stamps a short
request id on ``g.request_id``; an ``after_request`` hook logs method/path/
status for every non-static request; 404/500/generic exception handlers
return JSON error envelopes. Most routes are additionally guarded by the
``auth.login_required`` / ``auth.admin_required`` / ``auth.dev_or_admin_required``
decorators from ``core/auth.py``.

Route groups defined in this file (non-exhaustive — see blueprints for the
rest): auth (login/OTP/logout/password), admin user & token-quota
management, org (departments/projects) admin + context, portal sync
admin endpoints, BI agent CRUD + chat + sessions + conversation history,
dashboard/report/infographic/PPT generation, client config CRUD, and email
intelligence endpoints used by the presentation generator.

When run directly (``__main__``), it also starts ``scheduler_service`` and
launches the Flask dev server.
"""
import os
import sys

# ── Package paths: allows flat imports from any subdirectory ──────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
for _pkg in ('core', 'database', 'services', 'generators', 'nlq', 'blueprints'):
    _p = os.path.join(_BASE, _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

from dotenv import load_dotenv
load_dotenv()   # must run before any import that reads os.getenv (config.py, etc.)

import uuid
from flask import Flask, render_template, jsonify, request, make_response, redirect, url_for, g
from database_manager import db_manager
from agent_manager import agent_manager
from nlq_engine import NLQEngine
import time
from dashboard_generator import generate_dashboard_config
from reportgenerator import generate_report_config, get_report_data_with_filters
from infographicgenerator import InfographicGenerator
import pandas as pd
from flask import Response 
import json 
import math
from ppt_generator import PresentationGenerator

import json as _json
import sys
from datetime import datetime, date
from app_jobs_routes import register_jobs_routes
from scheduler_service import scheduler_service
from workspace_bp import workspace_bp
from training_bp import training_bp
from agents_hub_bp import agents_hub_bp
from knowledge_bp import knowledge_bp
from admin_conversations_bp import admin_conversations_bp
import bi_training_db as _bi_td
import bi_conversations_db as _bi_conv_db
import threading

from email_intelligence import (
    get_senders_for_client, get_emails,
    parse_date_range_from_question, format_emails_for_llm,
    resolve_client_db_cfg,
)

# ── AUTH + TOKEN LIMITS ───────────────────────────────────────
import config
import auth
import token_limits
import org_db
import nexus_sync_db
# ─────────────────────────────────────────────────────────────

# ── APP DATABASE SETUP (tables + JSON migration) ──────────────
import app_db as _app_db
_app_db.ensure_tables()
_app_db.migrate_from_json()
_app_db.ensure_portal_views()   # cross-database views → external portal
import workspace_db as _ws_db; _ws_db.ensure_schema()
# ─────────────────────────────────────────────────────────────

# ── LOGGING SETUP (must be before service startups) ───────────
import logging
from logging_config import setup_logging
logger = setup_logging(env=os.getenv("FLASK_ENV", "development"))
# ─────────────────────────────────────────────────────────────

# ── DOCUMENT WATCHER SERVICE ───────────────────────────────────
try:
    from services.document_watcher import get_watcher as _get_watcher
    _doc_watcher = _get_watcher()
    _doc_watcher.start()
except Exception as _we:
    logger.warning("document_watcher: failed to start: %s", _we)
# ─────────────────────────────────────────────────────────────

# ── SHAREPOINT WATCHER SERVICE ─────────────────────────────────
try:
    from services.sharepoint_watcher import get_sp_watcher as _get_sp_watcher
    _sp_watcher = _get_sp_watcher()
    _sp_watcher.start()
except Exception as _spe:
    logger.warning("sharepoint_watcher: failed to start: %s", _spe)
# ─────────────────────────────────────────────────────────────




app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.logger.handlers = logger.handlers
app.logger.setLevel(logging.INFO)
register_jobs_routes(app)
app.register_blueprint(workspace_bp)
app.register_blueprint(training_bp)
app.register_blueprint(agents_hub_bp)
app.register_blueprint(knowledge_bp)
app.register_blueprint(admin_conversations_bp)

# ── SENTRY ERROR TRACKING ─────────────────────────────────────
from sentry_config import init_sentry, set_user_context as _sentry_set_user
init_sentry(env=config.ENV)
# ─────────────────────────────────────────────────────────────

# ── MICROSOFT TEAMS ALERTS ────────────────────────────────────
from teams_notifier import TeamsAlertHandler
if os.getenv("TEAMS_WEBHOOK_URL"):
    _teams_handler = TeamsAlertHandler(level=logging.ERROR)
    logger.addHandler(_teams_handler)
    logger.info("Teams alert handler active")
# ─────────────────────────────────────────────────────────────

# ── Auth hooks ────────────────────────────────────────────────
@app.before_request
def load_user():
    """Populate g.current_user from the session cookie and stamp a short request id on g.request_id."""
    auth.load_current_user()
    g.request_id = str(uuid.uuid4())[:8]

_req_logger = logging.getLogger("app.requests")

@app.after_request
def _log_request(response):
    """Log method/path/status for every non-static request, tagged with the request id."""
    if not request.path.startswith("/static"):
        rid = getattr(g, "request_id", "-")
        _req_logger.info("[%s] %s %s → %s", rid, request.method, request.path, response.status_code)
    return response

APPROVAL_ENABLED = os.getenv('APPROVAL', 'false').lower() == 'true'


@app.context_processor
def inject_user():
    """Expose current_user and approval_enabled as globals in every Jinja template."""
    return {"current_user": auth.current_user(), "approval_enabled": APPROVAL_ENABLED}
# ─────────────────────────────────────────────────────────────

# ── GLOBAL ERROR HANDLERS ─────────────────────────────────────
@app.errorhandler(404)
def _handle_404(err):
    """Return a JSON 404 envelope for unmatched routes."""
    logger.warning("404 %s %s", request.method, request.path)
    return jsonify({"status": "error", "message": "Not found"}), 404

@app.errorhandler(500)
def _handle_500(err):
    """Return a JSON 500 envelope and log the full traceback with the request id."""
    rid = getattr(g, "request_id", "-")
    logger.exception("500 %s %s [rid=%s]", request.method, request.path, rid)
    return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.errorhandler(Exception)
def _handle_unhandled(exc):
    """Catch-all handler for any exception not covered by a more specific handler."""
    rid = getattr(g, "request_id", "-")
    logger.exception("Unhandled exception %s %s [rid=%s]", request.method, request.path, rid)
    return jsonify({"status": "error", "message": "Unexpected server error"}), 500
# ─────────────────────────────────────────────────────────────

# ========== SINGLE ENGINE INITIALIZATION ==========
nlq_engine = NLQEngine(
    openai_api_key=os.getenv('OPENAI_API_KEY'),
    database_manager=db_manager
)
logger.info(" NLQ Engine initialized with caching on startup")

# ========== ROUTE REGISTRATION (Phase 3 Slice 2 — moved out of app.py) ====
# Route handler bodies live in blueprints/app_*_routes.py; each register_*
# function decorates directly onto this `app` object with @app.route (same
# pattern as register_jobs_routes above), so endpoint names and URL paths
# are byte-for-byte identical to before this reorganization.
from app_auth_admin_routes import register_auth_admin_routes
from app_core_pages_routes import register_core_pages_routes
from app_bi_agents_routes import register_bi_agents_routes
from app_bi_generation_routes import register_bi_generation_routes

register_auth_admin_routes(app)
register_core_pages_routes(app, nlq_engine=nlq_engine, agent_manager=agent_manager, db_manager=db_manager)
register_bi_agents_routes(app, nlq_engine=nlq_engine, agent_manager=agent_manager, db_manager=db_manager)

_ppt_gen = PresentationGenerator()
logger.info("PresentationGenerator ready")
register_bi_generation_routes(app, db_manager=db_manager, _ppt_gen=_ppt_gen)


if __name__ == '__main__':
    scheduler_service.start()
    logger.info("Starting Flask application...")
    app.run(debug=True, use_reloader=False)