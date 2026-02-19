"""admin_conversations_bp.py — Admin "Conversation Supervisor" Flask Blueprint.

Lets admins see who asked what: browse any user's chat history across all
three chat systems in this app (Workspace AI chat, Agent Hub chat, BI/NLQ
chat). All three already store who (user_id), what (message content) and
when (created_at) — this blueprint just exposes an admin-only read path
across the ownership checks that normally scope each system to its own user.

Register in app.py:
    from admin_conversations_bp import admin_conversations_bp
    app.register_blueprint(admin_conversations_bp)

Routes
------
  GET /admin/conversations                          — supervisor page (admin only)

  GET /api/admin/conversations/overview              — per-user chat activity summary (all 3 systems)
  GET /api/admin/conversations/workspace?user_id=    — a user's workspace conversations
  GET /api/admin/conversations/workspace/<id>        — full workspace conversation + messages
  GET /api/admin/conversations/hub?user_id=          — a user's Agent Hub conversations
  GET /api/admin/conversations/hub/<cid>             — full hub conversation + messages
  GET /api/admin/conversations/bi?user_id=           — a user's BI/NLQ chat conversations
  GET /api/admin/conversations/bi/<cid>              — full BI conversation + messages
"""

from flask import Blueprint, render_template, request, jsonify

import auth
import workspace_db
import bi_conversations_db
from app_db import get_app_db
from core.teams_crypto import teams_decrypt

admin_conversations_bp = Blueprint("admin_conversations", __name__)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _fix_row(row: dict) -> dict:
    """Stringify datetime fields so jsonify never chokes on them."""
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _fix_rows(rows: list[dict]) -> list[dict]:
    return [_fix_row(r) for r in rows]


def _ss_query(sql: str, params: tuple = (), fetchall: bool = False, fetchone: bool = False):
    """Run a query against the shared SQL Server app database."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        if fetchall:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        if fetchone:
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


# ─────────────────────────────────────────────────────────────
# PAGE
# ─────────────────────────────────────────────────────────────

@admin_conversations_bp.route("/admin/conversations")
@auth.login_required
@auth.admin_required
def admin_conversations_page():
    """Render the conversation-supervisor page."""
    return render_template("admin_conversations.html")


# ─────────────────────────────────────────────────────────────
# API — OVERVIEW (who's been chatting, across all 3 systems)
# ─────────────────────────────────────────────────────────────

@admin_conversations_bp.route("/api/admin/conversations/overview", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_conversations_overview():
    """Return per-user chat activity counts across Workspace/Hub/BI chat, newest activity first."""
    users = auth.get_all_users()

    ws = _ss_query("""
        SELECT c.user_id, COUNT(DISTINCT c.id) AS conversation_count,
               COUNT(m.id) AS message_count, MAX(m.created_at) AS last_message_at
        FROM   ws_conversations c
        JOIN   ws_messages m ON m.conversation_id = c.id
        GROUP BY c.user_id
    """, fetchall=True) or []

    hub = _ss_query("""
        SELECT c.user_id, COUNT(DISTINCT c.id) AS conversation_count,
               COUNT(m.id) AS message_count, MAX(m.created_at) AS last_message_at
        FROM   hub_conversations c
        JOIN   hub_messages m ON m.conversation_id = c.id
        GROUP BY c.user_id
    """, fetchall=True) or []

    bi = _ss_query("""
        SELECT c.user_id, COUNT(DISTINCT c.id) AS conversation_count,
               COUNT(m.id) AS message_count, MAX(m.created_at) AS last_message_at
        FROM   bi_conversations c
        JOIN   bi_messages m ON m.conversation_id = c.id
        GROUP BY c.user_id
    """, fetchall=True) or []

    by_user = {
        u["id"]: {
            "user_id": u["id"], "username": u["username"], "email": u["email"], "role": u["role"],
            "workspace": {"conversations": 0, "messages": 0, "last_activity": None},
            "hub":       {"conversations": 0, "messages": 0, "last_activity": None},
            "bi":        {"conversations": 0, "messages": 0, "last_activity": None},
        }
        for u in users
    }

    for rows, key in ((ws, "workspace"), (hub, "hub"), (bi, "bi")):
        for r in rows:
            entry = by_user.get(r["user_id"])
            if not entry:
                continue
            entry[key] = {
                "conversations": r["conversation_count"],
                "messages": r["message_count"],
                "last_activity": r["last_message_at"].isoformat() if r["last_message_at"] else None,
            }

    result = [
        u for u in by_user.values()
        if u["workspace"]["messages"] or u["hub"]["messages"] or u["bi"]["messages"]
    ]

    def _latest(u):
        dates = [u[k]["last_activity"] for k in ("workspace", "hub", "bi") if u[k]["last_activity"]]
        return max(dates) if dates else ""

    result.sort(key=_latest, reverse=True)
    return jsonify({"status": "ok", "users": result})


# ─────────────────────────────────────────────────────────────
# API — WORKSPACE CHAT
# ─────────────────────────────────────────────────────────────

@admin_conversations_bp.route("/api/admin/conversations/workspace", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_admin_workspace_conversations():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400
    convs = workspace_db.get_conversations(user_id, include_archived=True, limit=200)
    return jsonify({"status": "ok", "conversations": convs})


@admin_conversations_bp.route("/api/admin/conversations/workspace/<int:conv_id>", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_admin_workspace_conversation(conv_id):
    conv = workspace_db.admin_get_conversation(conv_id)
    if not conv:
        return jsonify({"status": "error", "message": "Not found"}), 404
    messages = workspace_db.get_conversation_messages(conv_id)
    for msg in messages:
        msg["content"] = teams_decrypt(msg["content"] or "")  # no-op if not encrypted
    return jsonify({"status": "ok", "conversation": conv, "messages": messages})


# ─────────────────────────────────────────────────────────────
# API — AGENT HUB CHAT
# ─────────────────────────────────────────────────────────────

@admin_conversations_bp.route("/api/admin/conversations/hub", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_admin_hub_conversations():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400
    rows = _ss_query("""
        SELECT c.*, a.name AS agent_name,
               (SELECT COUNT(*) FROM hub_messages m WHERE m.conversation_id = c.id) AS message_count
        FROM   hub_conversations c
        LEFT JOIN hub_agents a ON a.id = c.agent_id
        WHERE  c.user_id = ?
        ORDER BY c.updated_at DESC
    """, (user_id,), fetchall=True) or []
    return jsonify({"status": "ok", "conversations": _fix_rows(rows)})


@admin_conversations_bp.route("/api/admin/conversations/hub/<cid>", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_admin_hub_conversation(cid):
    conv = _ss_query("SELECT * FROM hub_conversations WHERE id=?", (cid,), fetchone=True)
    if not conv:
        return jsonify({"status": "error", "message": "Not found"}), 404
    rows = _ss_query(
        "SELECT * FROM hub_messages WHERE conversation_id=? ORDER BY created_at", (cid,), fetchall=True
    ) or []
    messages = []
    for r in rows:
        d = _fix_row(r)
        d["content"] = teams_decrypt(d.get("content") or "")  # no-op if not encrypted
        messages.append(d)
    return jsonify({"status": "ok", "conversation": _fix_row(conv), "messages": messages})


# ─────────────────────────────────────────────────────────────
# API — BI / NLQ CHAT
# ─────────────────────────────────────────────────────────────

@admin_conversations_bp.route("/api/admin/conversations/bi", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_admin_bi_conversations():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"status": "error", "message": "user_id is required"}), 400
    convs = bi_conversations_db.list_conversations(user_id, is_admin=False, limit=200)
    return jsonify({"status": "ok", "conversations": convs})


@admin_conversations_bp.route("/api/admin/conversations/bi/<cid>", methods=["GET"])
@auth.login_required
@auth.admin_required
def api_admin_bi_conversation(cid):
    conv = bi_conversations_db.get_conversation(cid, is_admin=True)
    if not conv:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "ok", "conversation": conv})
