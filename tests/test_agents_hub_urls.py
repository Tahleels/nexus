"""Protects two hidden coupling points found by reading the actual code:

1. Templates call url_for('agents_hub.<endpoint>') directly (21 occurrences
   across dashboard.html, components/sidebar.html,
   agents_hub/hub_tool_job_monitor.html). If the blueprint name or a page
   endpoint's function name changes, Jinja raises BuildError at render time.
2. A representative status-code matrix across auth levels, so a route moved
   into a different file without its decorator (or renamed without updating
   a frontend fetch() URL) shows up as a wrong status code here instead of
   a silent 404/403 in the browser.
"""
import pytest

# Every agents_hub page endpoint referenced via url_for() in templates —
# confirmed by grepping templates/ directly, not assumed.
TEMPLATE_REFERENCED_PAGE_ENDPOINTS = [
    "agents_hub.hub_chat_page",
    "agents_hub.approvals_page",
    "agents_hub.hub_agents_page",
    "agents_hub.hub_workflows_page",
    "agents_hub.hub_tools_page",
    "agents_hub.custom_tools_page",
    "agents_hub.hub_knowledge_page",
    "agents_hub.hub_analytics_page",
    "agents_hub.hub_agent_activity_page",
    "agents_hub.hub_jobs_page",
    "agents_hub.hub_tool_jobs_page",
    "agents_hub.hub_tool_job_monitor_page",
    "agents_hub.hub_assignments_page",
]


@pytest.mark.parametrize("endpoint", TEMPLATE_REFERENCED_PAGE_ENDPOINTS)
def test_url_for_resolves(flask_app, endpoint):
    with flask_app.test_request_context():
        from flask import url_for
        url_for(endpoint)  # raises werkzeug.routing.BuildError if broken


def test_page_route_requires_login(anon_client):
    resp = anon_client.get("/agents-hub")
    assert resp.status_code in (302, 401)


def test_page_route_ok_when_authed(admin_client):
    resp = admin_client.get("/agents-hub")
    assert resp.status_code == 200


@pytest.mark.parametrize("path", [
    "/api/agenthub/agents",
    "/api/agenthub/workflows",
    "/api/agenthub/approvals",
])
def test_login_only_api_unauth_rejected(anon_client, path):
    resp = anon_client.get(path)
    assert resp.status_code == 401


@pytest.mark.parametrize("path", [
    "/api/agenthub/agents",
    "/api/agenthub/workflows",
    "/api/agenthub/approvals",
])
def test_login_only_api_ok_for_any_role(user_client, path):
    resp = user_client.get(path)
    assert resp.status_code == 200


@pytest.mark.parametrize("path", [
    "/api/agenthub/jobs",
    "/api/agenthub/custom-tools",
    "/api/agenthub/tool-jobs",
])
def test_dev_or_admin_api_rejects_plain_user(user_client, path):
    resp = user_client.get(path)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", [
    "/api/agenthub/jobs",
    "/api/agenthub/custom-tools",
    "/api/agenthub/tool-jobs",
])
def test_dev_or_admin_api_ok_for_dev(dev_client, path):
    resp = dev_client.get(path)
    assert resp.status_code == 200


@pytest.mark.parametrize("path", [
    "/api/agenthub/jobs",
    "/api/agenthub/custom-tools",
    "/api/agenthub/tool-jobs",
])
def test_dev_or_admin_api_ok_for_admin(admin_client, path):
    resp = admin_client.get(path)
    assert resp.status_code == 200
