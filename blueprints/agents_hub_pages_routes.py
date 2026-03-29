"""Page/HTML routes for the Hub Agents blueprint (agents_hub_bp): every
route that just renders a template. Split out of agents_hub_bp.py in
Phase 3 Slice 4 ("Stage B" of the agents_hub_bp.py de-monolith).
"""

import sys, os, uuid, json, logging, threading, re, calendar
from datetime import datetime, timedelta, date
from flask import (render_template, jsonify, request,
                   Response, stream_with_context, redirect, url_for, abort)
import auth
import token_limits
from agents_hub_bp import agents_hub_bp, _APPROVAL_ENABLED


@agents_hub_bp.route('/agents-hub')
@auth.login_required
def hub_chat_page():
    return render_template('agents_hub/chat.html')


@agents_hub_bp.route('/preview/document')
@auth.login_required
def preview_document():
    return render_template('documentlayout.html')


@agents_hub_bp.route('/agents-hub/workflows')
@auth.login_required
@auth.dev_or_admin_required
def hub_workflows_page():
    return render_template('agents_hub/hub_workflows.html')


@agents_hub_bp.route('/agents-hub/agents')
@auth.login_required
@auth.dev_or_admin_required
def hub_agents_page():
    return render_template('agents_hub/hub_agents.html')


@agents_hub_bp.route('/agents-hub/tools')
@auth.login_required
@auth.dev_or_admin_required
def hub_tools_page():
    return render_template('agents_hub/hub_tools.html')


@agents_hub_bp.route('/agents-hub/knowledge')
@auth.login_required
@auth.dev_or_admin_required
def hub_knowledge_page():
    return render_template('agents_hub/hub_knowledge.html')


@agents_hub_bp.route('/agents-hub/jobs')
@auth.login_required
@auth.dev_or_admin_required
def hub_jobs_page():
    return render_template('agents_hub/hub_jobs.html')


@agents_hub_bp.route('/agents-hub/agent-activity')
@auth.login_required
@auth.dev_or_admin_required
def hub_agent_activity_page():
    return render_template('agents_hub/hub_agent_activity.html')


@agents_hub_bp.route('/agents-hub/analytics')
@auth.login_required
@auth.dev_or_admin_required
def hub_analytics_page():
    return render_template('agents_hub/hub_analytics.html')


@agents_hub_bp.route('/agents-hub/dashboard')
@auth.login_required
@auth.dev_or_admin_required
def hub_dashboard_page():
    return render_template('agents_hub/hub_dashboard.html')


@agents_hub_bp.route('/agents-hub/assignments')
@auth.login_required
@auth.dev_or_admin_required
def hub_assignments_page():
    return render_template('agents_hub/hub_assignments.html')


@agents_hub_bp.route('/approvals')
@auth.login_required
def approvals_page():
    if not _APPROVAL_ENABLED:
        abort(404)
    return render_template('approvals.html')


@agents_hub_bp.route('/agents-hub/custom-tools')
@auth.login_required
@auth.dev_or_admin_required
def custom_tools_page():
    return render_template('agents_hub/hub_custom_tools.html')


@agents_hub_bp.route('/agents-hub/tool-jobs')
@auth.login_required
@auth.dev_or_admin_required
def hub_tool_jobs_page():
    return render_template('agents_hub/hub_tool_jobs.html')


@agents_hub_bp.route('/agents-hub/tool-job-monitor')
@auth.login_required
@auth.dev_or_admin_required
def hub_tool_job_monitor_page():
    return render_template('agents_hub/hub_tool_job_monitor.html')
