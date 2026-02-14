# =============================================================
# auth.py  —  Authentication & Authorization module
# =============================================================
# Responsibilities:
#   - DB access for users / sessions
#   - Password hashing (bcrypt)
#   - Session management (server-side, stored in SQL Server)
#   - Role-based access decorators
# =============================================================
"""auth.py — Authentication and authorization for the Nexus AI Portal.

Implements the full login lifecycle used by app.py and every blueprint:
    1. Username/password check (bcrypt) → store_otp() + send_otp_email()
    2. OTP verification (in-process, TTL-bound) → create_session()
    3. Session token stored server-side in SQL Server (``user_sessions``)
       and handed to the browser as an httponly cookie
    4. Each request's ``before_request`` hook calls load_current_user(),
       which validates the cookie token and populates ``flask.g.current_user``
    5. Route-level RBAC via the login_required / admin_required /
       dev_or_admin_required / roles_required decorators

Also owns CRUD for users, agent assignments, and per-user token quotas.
All DB access goes through ``app_db.get_app_db()`` — the single pyodbc
connection helper shared with every other ``database/*.py`` module.

Note: "dev proxy" (a dev/admin temporarily acting as another user) is
configured and authorised in ``database/org_db.py`` (``dev_proxy_assignments``
table); this module only supplies the underlying session primitives that
a proxy login would reuse.
"""

import pyodbc
import bcrypt
import secrets
import smtplib
import random
import threading
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import request, jsonify, session, redirect, url_for, g
from logging_config import get_logger

import config   # ← your config.py
from app_db import get_app_db as _get_db  # single source of truth for connections

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# OTP STORE  (in-process; survives the request lifecycle)
# ─────────────────────────────────────────────────────────────

_otp_store: dict[int, dict] = {}  # user_id → {otp, expires_at}
_otp_lock = threading.Lock()


def _generate_otp() -> str:
    """Return a cryptographically random 6-digit OTP string (zero-padded)."""
    return f"{random.SystemRandom().randint(0, 999999):06d}"


def store_otp(user_id: int) -> str:
    """Generate a new OTP for user_id, store it in-process with a TTL, and return it."""
    otp = _generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=config.OTP_EXPIRY_MINUTES)
    with _otp_lock:
        _otp_store[user_id] = {"otp": otp, "expires_at": expires_at}
    return otp


def verify_otp(user_id: int, otp: str) -> bool:
    """Check otp against the stored value for user_id; consumes it (single use) on match or expiry."""
    with _otp_lock:
        entry = _otp_store.get(user_id)
        if not entry:
            return False
        if datetime.now(timezone.utc) > entry["expires_at"]:
            _otp_store.pop(user_id, None)
            return False
        if entry["otp"] != otp:
            return False
        _otp_store.pop(user_id, None)
        return True


def send_otp_email(to_email: str, otp: str, username: str) -> bool:
    """Send OTP via SMTP configured in env. Returns True on success."""
    if not config.SMTP_USERNAME or not config.SMTP_PASSWORD:
        logger.error("SMTP credentials not configured — cannot send OTP")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your Nexus Sign-In Code"
        msg["From"]    = config.SMTP_FROM
        msg["To"]      = to_email

        text = (
            f"Hi {username},\n\n"
            f"Your one-time sign-in code is: {otp}\n\n"
            f"This code expires in {config.OTP_EXPIRY_MINUTES} minutes.\n"
            f"If you did not request this, ignore this email.\n\n"
            f"— Nexus"
        )
        html = f"""
        <html><body style="font-family:sans-serif;color:#1e293b;max-width:480px;margin:auto">
          <h2 style="color:#4f46e5">Nexus</h2>
          <p>Hi <strong>{username}</strong>,</p>
          <p>Use the code below to complete your sign-in:</p>
          <div style="font-size:2rem;font-weight:700;letter-spacing:.4rem;
                      background:#f1f5f9;border-radius:8px;padding:1rem 2rem;
                      text-align:center;color:#4f46e5">{otp}</div>
          <p style="color:#64748b;font-size:.85rem">
            Expires in {config.OTP_EXPIRY_MINUTES} minutes.
            If you didn't request this, you can safely ignore this email.
          </p>
        </body></html>
        """
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html,  "html"))

        if config.SMTP_USE_TLS:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT)
        server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_FROM, to_email, msg.as_string())
        server.quit()
        logger.info(f"OTP sent to {to_email}")
        return True
    except Exception:
        logger.exception(f"Failed to send OTP email to {to_email}")
        return False


# ─────────────────────────────────────────────────────────────
# PASSWORD HELPERS
# ─────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Return a bcrypt hash (cost factor 12) of the plaintext password."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the given bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ─────────────────────────────────────────────────────────────
# USER QUERIES
# ─────────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> dict | None:
    """Return the user row (with joined role name) for username, or None if not found."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.email, u.password_hash,
                   u.is_active, u.created_at, u.last_login,
                   u.token_quota, r.name AS role
            FROM   users u
            JOIN   roles r ON r.id = u.role_id
            WHERE  u.username = ?
        """, username)
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def get_user_by_email(email: str) -> dict | None:
    """Return the user row (with joined role name) for email, or None if not found."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.email, u.password_hash,
                   u.is_active, u.created_at, u.last_login,
                   u.token_quota, r.name AS role
            FROM   users u
            JOIN   roles r ON r.id = u.role_id
            WHERE  u.email = ?
        """, email)
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def get_user_by_id(user_id: int) -> dict | None:
    """Return the user row (with joined role name; no password_hash) for user_id, or None."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.email,
                   u.is_active, u.created_at, u.last_login,
                   u.token_quota, r.name AS role
            FROM   users u
            JOIN   roles r ON r.id = u.role_id
            WHERE  u.id = ?
        """, user_id)
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def get_all_users() -> list[dict]:
    """Return every user (with role name), newest first. Used by the admin users page."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, u.email,
                   u.is_active, u.created_at, u.last_login,
                   r.name AS role, u.created_by, u.token_quota
            FROM   users u
            JOIN   roles r ON r.id = u.role_id
            ORDER BY u.created_at DESC
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def set_user_token_quota(user_id: int, quota: int | None) -> tuple[bool, str]:
    """Set a custom daily token quota for a user. Pass None to reset to role default."""
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET token_quota = ? WHERE id = ?", quota, user_id)
            conn.commit()
        return True, "Token quota updated"
    except Exception:
        logger.exception("set_user_token_quota failed")
        return False, "Internal error updating token quota"


def create_user(username: str, email: str, plain_password: str,
                role_name: str, created_by: int) -> tuple[bool, str, int | None]:
    """Returns (success, message, new_user_id)."""
    try:
        pw_hash = hash_password(plain_password)
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM roles WHERE name = ?", role_name)
            role_row = cursor.fetchone()
            if not role_row:
                return False, f"Unknown role: {role_name}", None
            role_id = role_row[0]

            cursor.execute("""
                INSERT INTO users (username, email, password_hash, role_id, created_by)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?)
            """, username, email, pw_hash, role_id, created_by)
            row = cursor.fetchone()
            new_id = row[0] if row else None
            conn.commit()
        return True, "User created successfully", new_id
    except pyodbc.IntegrityError as e:
        return False, "Username or email already exists", None
    except Exception as e:
        logger.exception("create_user failed")
        return False, "Internal error creating user", None


def update_user(user_id: int, data: dict) -> tuple[bool, str]:
    """Update email, role, or active status. Pass only keys you want to change."""
    allowed = {"email", "role_name", "is_active"}
    try:
        with _get_db() as conn:
            cursor = conn.cursor()

            if "role_name" in data:
                cursor.execute("SELECT id FROM roles WHERE name = ?", data["role_name"])
                row = cursor.fetchone()
                if not row:
                    return False, f"Unknown role: {data['role_name']}"
                cursor.execute("UPDATE users SET role_id = ? WHERE id = ?", row[0], user_id)

            if "email" in data:
                cursor.execute("UPDATE users SET email = ? WHERE id = ?", data["email"], user_id)

            if "is_active" in data:
                cursor.execute("UPDATE users SET is_active = ? WHERE id = ?",
                               int(data["is_active"]), user_id)

            conn.commit()

        if "email" in data:
            try:
                import nexus_sync_db
                nexus_sync_db.sync_user_by_email(user_id, data["email"], assigned_by=user_id)
            except Exception:
                logger.exception("update_user: portal re-sync after email change failed")

        return True, "User updated"
    except Exception as e:
        logger.exception("update_user failed")
        return False, "Internal error updating user"


def reset_password(user_id: int, new_plain: str) -> tuple[bool, str]:
    try:
        pw_hash = hash_password(new_plain)
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", pw_hash, user_id)
            conn.commit()
        return True, "Password updated"
    except Exception as e:
        logger.exception("reset_password failed")
        return False, "Internal error resetting password"


def delete_user(user_id: int) -> tuple[bool, str]:
    try:
        with _get_db() as conn:
            cursor = conn.cursor()

            # Step 1: delete leaf ws_* tables that hold a direct user_id FK
            for tbl, col in [
                ("ws_feedback",              "user_id"),
                ("ws_message_edits",         "user_id"),
                ("ws_model_usage",           "user_id"),
                ("ws_user_settings",         "user_id"),
                ("ws_activity_log",          "user_id"),
                ("ws_retry_log",             "user_id"),
                ("ws_copy_log",              "user_id"),
                ("ws_artifacts",             "user_id"),
                ("ws_files",                 "user_id"),
                ("ws_prompt_library",        "user_id"),
                ("ws_enterprise_memory",     "user_id"),
                ("ws_workspace_members",     "user_id"),
                ("app_document_assignments", "user_id"),
                ("agent_guardrails",         "user_id"),
            ]:
                cursor.execute(f"DELETE FROM {tbl} WHERE {col} = ?", user_id)

            # Step 2: nullify refs (from other users' rows) pointing at this user's messages
            cursor.execute(
                "UPDATE ws_artifacts SET message_id = NULL "
                "WHERE message_id IN (SELECT id FROM ws_messages WHERE user_id = ?)", user_id)
            cursor.execute(
                "UPDATE ws_training_pairs SET message_id = NULL "
                "WHERE message_id IN (SELECT id FROM ws_messages WHERE user_id = ?)", user_id)

            # Step 3: delete messages sent by this user (cascades chunks/tool_calls/citations)
            cursor.execute("DELETE FROM ws_messages WHERE user_id = ?", user_id)

            # Step 4: nullify refs pointing at this user's conversations
            cursor.execute(
                "UPDATE ws_artifacts SET conversation_id = NULL, message_id = NULL "
                "WHERE conversation_id IN (SELECT id FROM ws_conversations WHERE user_id = ?)", user_id)
            cursor.execute(
                "UPDATE ws_training_pairs SET conversation_id = NULL "
                "WHERE conversation_id IN (SELECT id FROM ws_conversations WHERE user_id = ?)", user_id)
            cursor.execute(
                "UPDATE ws_training_preference_pairs SET conversation_id = NULL "
                "WHERE conversation_id IN (SELECT id FROM ws_conversations WHERE user_id = ?)", user_id)
            cursor.execute(
                "UPDATE ws_conversations SET project_id = NULL "
                "WHERE project_id IN (SELECT id FROM ws_projects WHERE owner_id = ?)", user_id)

            # Step 5: delete this user's conversations (cascades remaining messages in them)
            cursor.execute("DELETE FROM ws_conversations WHERE user_id = ?", user_id)

            # Step 6: delete this user's projects (conversations already de-referenced)
            cursor.execute("DELETE FROM ws_projects WHERE owner_id = ?", user_id)

            # Step 7: nullify workspace refs before removing the user's workspaces
            for tbl, col in [
                ("ws_training_pairs",    "workspace_id"),
                ("ws_projects",          "workspace_id"),
                ("ws_conversations",     "workspace_id"),
                ("ws_prompt_library",    "workspace_id"),
                ("ws_enterprise_memory", "workspace_id"),
            ]:
                cursor.execute(
                    f"UPDATE {tbl} SET {col} = NULL "
                    f"WHERE {col} IN (SELECT id FROM ws_workspaces WHERE owner_id = ?)", user_id)

            # Step 8: delete this user's workspaces (cascades ws_workspace_members)
            cursor.execute("DELETE FROM ws_workspaces WHERE owner_id = ?", user_id)

            # Step 9: clean up bi / hub conversation data (data hygiene; may lack FK)
            cursor.execute(
                "DELETE FROM bi_messages "
                "WHERE conversation_id IN (SELECT id FROM bi_conversations WHERE user_id = ?)", user_id)
            cursor.execute("DELETE FROM bi_conversations WHERE user_id = ?", user_id)

            # Step 10: nullify/delete all rows that reference this user directly
            for tbl, col in [
                ("ws_training_pairs",            "reviewed_by"),
                ("ws_training_preference_pairs", "reviewed_by"),
                ("bi_training_pairs",            "user_id"),
                ("bi_training_pairs",            "reviewed_by"),
            ]:
                cursor.execute(f"UPDATE {tbl} SET {col} = NULL WHERE {col} = ?", user_id)
            cursor.execute(
                "DELETE FROM ws_training_annotations WHERE annotated_by = ?", user_id)
            cursor.execute(
                "DELETE FROM ws_training_exports WHERE exported_by = ?", user_id)

            # Step 11: delete the user row (user_sessions/agent_assignments/token_usage cascade)
            cursor.execute("DELETE FROM users WHERE id = ?", user_id)
            conn.commit()
        return True, "User deleted"
    except Exception as e:
        logger.exception("delete_user failed")
        return False, "Internal error deleting user"


# ─────────────────────────────────────────────────────────────
# SESSION MANAGEMENT  (server-side tokens in SQL Server)
# ─────────────────────────────────────────────────────────────

SESSION_TTL_HOURS = config.PERMANENT_SESSION_HOURS


def create_session(user_id: int, ip: str, ua: str) -> str:
    """Create a new server-side session row and return the opaque bearer token."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_sessions (user_id, token, ip_address, user_agent, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, user_id, token, ip[:45], ua[:512], expires_at)
        conn.commit()
    return token


def validate_session(token: str) -> dict | None:
    """Return user dict if token is valid and not expired, else None."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.user_id, s.expires_at
            FROM   user_sessions s
            WHERE  s.token = ? AND s.is_active = 1
        """, token)
        row = cursor.fetchone()
        if not row:
            return None
        user_id, expires_at = row

        # SQL Server returns naive datetime; treat as UTC
        if expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            invalidate_session(token)
            return None

    return get_user_by_id(user_id)


def invalidate_session(token: str) -> None:
    """Mark a single session token inactive (logout)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE user_sessions SET is_active = 0 WHERE token = ?", token)
        conn.commit()


def invalidate_all_user_sessions(user_id: int) -> None:
    """Mark every active session for user_id inactive (force logout everywhere)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE user_sessions SET is_active = 0 WHERE user_id = ?", user_id)
        conn.commit()


def update_last_login(user_id: int) -> None:
    """Stamp users.last_login with the current UTC time."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_login = GETUTCDATE() WHERE id = ?", user_id)
        conn.commit()


# ─────────────────────────────────────────────────────────────
# REQUEST-LEVEL CURRENT USER  (stored in Flask g)
# ─────────────────────────────────────────────────────────────
COOKIE_NAME = config.SESSION_COOKIE_NAME


def load_current_user():
    """
    Call this in a before_request hook.
    Reads the session token from the cookie and populates g.current_user.
    """
    g.current_user = None
    token = request.cookies.get(COOKIE_NAME)
    if token:
        user = validate_session(token)
        if user:
            g.current_user = user


def current_user() -> dict | None:
    """Return the current request's authenticated user dict, or None if not logged in."""
    return getattr(g, "current_user", None)


# ─────────────────────────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────────────────────────

def _is_api_route() -> bool:
    """True if the current request path is under /api/ (controls JSON 401/403 vs HTML redirect)."""
    return request.path.startswith("/api/")


def login_required(f):
    """Blocks unauthenticated access. Returns 401 for API, redirect for pages."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            if _is_api_route():
                return jsonify({"status": "error", "message": "Authentication required"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def roles_required(*allowed_roles):
    """
    Usage:
        @roles_required("admin")
        @roles_required("admin", "dev")
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = current_user()
            if not user:
                if _is_api_route():
                    return jsonify({"status": "error", "message": "Authentication required"}), 401
                return redirect(url_for("login_page"))
            if user["role"] not in allowed_roles:
                if _is_api_route():
                    return jsonify({"status": "error",
                                    "message": "You don't have permission to access this resource"}), 403
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)   # ← was missing
        return decorated
    return decorator


def admin_required(f):
    """Decorator: restrict a route to users with role == 'admin'."""
    return roles_required("admin")(f)


def dev_or_admin_required(f):
    """Decorator: restrict a route to users with role 'admin' or 'dev'."""
    return roles_required("admin", "dev")(f)


def user_in_department(user_id: int, dept_name: str) -> bool:
    """True if the user is assigned to the named department."""
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM user_departments ud
            JOIN departments d ON d.id = ud.dept_id
            WHERE ud.user_id = ? AND d.name = ?
        """, user_id, dept_name)
        return cur.fetchone() is not None


# ─────────────────────────────────────────────────────────────
# AGENT ASSIGNMENTS
# ─────────────────────────────────────────────────────────────

def get_assigned_agents(user_id: int) -> list[str]:
    """Return list of agent_names assigned to a user."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT agent_name FROM agent_assignments WHERE user_id = ?", user_id
        )
        return [row[0] for row in cursor.fetchall()]


def get_all_assignments() -> list[dict]:
    """Return all assignments with user info — for the admin panel."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT aa.id, aa.user_id, u.username, u.email,
                   r.name AS role, aa.agent_name, aa.assigned_at,
                   ab.username AS assigned_by_name
            FROM   agent_assignments aa
            JOIN   users u  ON u.id  = aa.user_id
            JOIN   roles r  ON r.id  = u.role_id
            JOIN   users ab ON ab.id = aa.assigned_by
            ORDER BY u.username, aa.agent_name
        """)
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        for row in rows:
            if row.get('assigned_at'):
                row['assigned_at'] = str(row['assigned_at'])
        return rows


def assign_agent(user_id: int, agent_name: str, assigned_by: int) -> tuple[bool, str]:
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM agent_assignments WHERE user_id = ? AND agent_name = ?
                )
                INSERT INTO agent_assignments (user_id, agent_name, assigned_by)
                VALUES (?, ?, ?)
            """, user_id, agent_name, user_id, agent_name, assigned_by)
            conn.commit()
        return True, "Agent assigned successfully"
    except Exception as e:
        logger.exception("assign_agent failed")
        return False, "Error assigning agent"


def unassign_agent(user_id: int, agent_name: str) -> tuple[bool, str]:
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM agent_assignments WHERE user_id = ? AND agent_name = ?",
                user_id, agent_name
            )
            conn.commit()
        return True, "Agent unassigned"
    except Exception as e:
        logger.exception("unassign_agent failed")
        return False, "Error unassigning agent"


def set_user_agents(user_id: int, agent_names: list[str], assigned_by: int) -> tuple[bool, str]:
    """Replace all assignments for a user with the given list."""
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_assignments WHERE user_id = ?", user_id)
            for name in agent_names:
                cursor.execute(
                    "INSERT INTO agent_assignments (user_id, agent_name, assigned_by) VALUES (?, ?, ?)",
                    user_id, name, assigned_by
                )
            conn.commit()
        return True, "Assignments updated"
    except Exception as e:
        logger.exception("set_user_agents failed")
        return False, "Error updating assignments"