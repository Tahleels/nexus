"""Cheapest, highest-value smoke test: does the app even import and boot?

Catches import-order/circular-import breakage from moving classes between
modules or splitting blueprints — the single most likely failure mode of
the agents_hub_bp.py de-monolith.
"""


def test_app_imports_and_boots(flask_app):
    assert flask_app is not None
    assert flask_app.name == "app"


def test_agents_hub_blueprint_registered(flask_app):
    assert "agents_hub" in flask_app.blueprints


def test_all_expected_blueprints_registered(flask_app):
    expected = {"agents_hub", "workspace", "training", "knowledge", "admin_conversations"}
    missing = expected - set(flask_app.blueprints.keys())
    assert not missing, f"missing blueprints: {missing}"


def test_agents_hub_route_count_sane(flask_app):
    rules = [r for r in flask_app.url_map.iter_rules() if r.endpoint.startswith("agents_hub.")]
    # Baseline is 85 routes at the time this suite was written — allow some
    # drift but catch a mass-deletion (routes silently dropped during a move).
    assert len(rules) >= 70, f"only {len(rules)} agents_hub routes registered, expected ~85"


def test_app_bare_route_count_sane(flask_app):
    """Guards Phase 3 Slice 2 (app.py's 89 bare routes moved into
    blueprints/app_*_routes.py). Bare = registered directly on `app`, not
    under a Blueprint prefix, so this counts everything NOT already covered
    by a known blueprint/registration-function prefix."""
    known_prefixed = ("agents_hub.", "workspace.", "training.", "knowledge.",
                       "admin_conversations.", "jobs.")
    bare_rules = [
        r for r in flask_app.url_map.iter_rules()
        if r.endpoint != "static" and not r.endpoint.startswith(known_prefixed)
    ]
    # Baseline is 90 bare URL rules (89 view functions; dashboard's '/' + '/dashboard'
    # decorators count as 2 rules for 1 function) at the time this was written.
    assert len(bare_rules) >= 85, f"only {len(bare_rules)} bare routes registered, expected ~90"
