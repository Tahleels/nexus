"""tests/conftest.py — smoke-test fixtures.

Loads .env.test (a disposable nexus_test database, see
LOCAL_TESTING_GUIDE.md for the setup steps) BEFORE `app` is imported, so
config.py / auth.py / app_db.py all pick up the test DB instead of dev's
nexus_local_dev. app.py's own `load_dotenv()` call (no override) leaves
these already-set env vars alone.

Auth is bypassed at the OTP layer, not mocked: fixtures seed real user rows
and mint real DB-backed session tokens via auth.create_session(), then set
them as the session cookie on the Flask test client. Every decorator
(login_required / admin_required / dev_or_admin_required) runs unmodified.
"""
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env.test", override=True)

# Mirror app.py's own sys.path setup so `import app` resolves the same way
# it does when run directly.
for _pkg in ("core", "database", "services", "generators", "nlq", "blueprints"):
    _p = str(_REPO_ROOT / _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="session")
def flask_app():
    """Import app.py once per test session (this boots the whole app)."""
    import app as app_module
    return app_module.app


@pytest.fixture(scope="session")
def app_module():
    import app as _app_module
    return _app_module


@pytest.fixture(scope="session")
def test_users(flask_app):
    """Seed one admin / dev / user-role account directly, once per session."""
    import app_db
    import bcrypt

    dummy_hash = bcrypt.hashpw(b"not-used-bypassed-by-tests", bcrypt.gensalt(4)).decode()

    users = {}
    with app_db.get_app_db() as conn:
        cur = conn.cursor()
        for role in ("admin", "dev", "user"):
            username = f"_pytest_{role}"
            email = f"_pytest_{role}@test.local"
            cur.execute("SELECT id FROM users WHERE username = ?", username)
            row = cur.fetchone()
            if row:
                users[role] = row[0]
                continue
            role_id = cur.execute("SELECT id FROM roles WHERE name = ?", role).fetchone()[0]
            cur.execute(
                """INSERT INTO users (username, email, password_hash, role_id, is_active)
                   OUTPUT INSERTED.id
                   VALUES (?, ?, ?, ?, 1)""",
                username, email, dummy_hash, role_id,
            )
            users[role] = cur.fetchone()[0]
        conn.commit()
    return users


def _client_with_session(flask_app, app_module, user_id):
    client = flask_app.test_client()
    token = app_module.auth.create_session(user_id, "127.0.0.1", "pytest")
    client.set_cookie(app_module.config.SESSION_COOKIE_NAME, token)
    return client


@pytest.fixture
def anon_client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def admin_client(flask_app, app_module, test_users):
    return _client_with_session(flask_app, app_module, test_users["admin"])


@pytest.fixture
def dev_client(flask_app, app_module, test_users):
    return _client_with_session(flask_app, app_module, test_users["dev"])


@pytest.fixture
def user_client(flask_app, app_module, test_users):
    return _client_with_session(flask_app, app_module, test_users["user"])
