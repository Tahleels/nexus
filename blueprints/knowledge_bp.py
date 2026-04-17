"""
knowledge_bp.py — Knowledge Base Flask blueprint.

Routes
------
  GET  /knowledge-base                          — page
  GET  /connector                               — directory connector + dev proxy page

  POST /api/knowledge/upload                    — upload + process a file
  GET  /api/knowledge/documents                 — list visible documents
  GET  /api/knowledge/documents/<id>            — document detail + chunk preview
  DELETE /api/knowledge/documents/<id>          — delete (owner or admin)
  POST /api/knowledge/documents/<id>/version    — upload new version
  GET  /api/knowledge/documents/<id>/assignments   — list assignments
  POST /api/knowledge/documents/<id>/assign        — assign to user(s)
  DELETE /api/knowledge/documents/<id>/assign/<uid> — remove assignment
  GET  /api/knowledge/users                     — list users (admin only)
  GET  /api/knowledge/colleagues                — list assignable users (any user; scoped to dept/project for non-admins)
  GET  /api/knowledge/my-departments             — departments the current user belongs to (any user)
  POST /api/knowledge/search                    — hybrid RAG search
  POST /api/knowledge/extract-text              — extract text without indexing

  GET  /api/knowledge/watch-dirs                — list watched directories
  POST /api/knowledge/watch-dirs                — add watched directory
  PUT  /api/knowledge/watch-dirs/<id>           — update watched directory
  DELETE /api/knowledge/watch-dirs/<id>         — remove watched directory
  POST /api/knowledge/watch-dirs/<id>/scan      — manual scan trigger

  GET  /api/dev/proxy/targets                   — list users dev can proxy as
  POST /api/dev/proxy/connect                   — set proxy user in session
  POST /api/dev/proxy/disconnect                — clear proxy from session
  GET  /api/admin/proxy-assignments             — list all proxy assignments (admin)
  POST /api/admin/proxy-assignments             — set proxy assignments for a dev (admin)

  GET  /api/knowledge/rag-traces                — retrieval trace log (admin)
"""
from logging_config import get_logger
import os
import uuid

from flask import Blueprint, jsonify, render_template, request, session

import auth as _auth

logger = get_logger(__name__)

knowledge_bp = Blueprint("knowledge", __name__)

_RAW_DIR = None

VALID_SCOPES = {"user", "global", "department", "project"}


def _get_raw_dir() -> str:
    """Return (and lazily create) the raw-uploaded-document storage directory."""
    global _RAW_DIR
    if _RAW_DIR is None:
        env_store = os.environ.get("AGENT_FILE_STORE", "").strip()
        store_dir = env_store if env_store else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Data", "agent_store")
        _RAW_DIR = os.path.join(store_dir, "raw_documents")
        os.makedirs(_RAW_DIR, exist_ok=True)
    return _RAW_DIR


def _current_user():
    """Return the real logged-in user dict (never the proxy target), or {} if unauthenticated."""
    return _auth.current_user() or {}


def _effective_user():
    """
    Return the effective (possibly proxied) user for resource attribution.
    When a dev/admin has connected as another user, resources are created
    for the proxy target but the real user keeps their original permissions.
    """
    real  = _auth.current_user() or {}
    proxy_id = session.get("proxy_user_id")
    if proxy_id and real.get("role") in ("admin", "dev"):
        try:
            from org_db import get_proxy_target_user
            target = get_proxy_target_user(int(proxy_id), int(real.get("id", 0)))
            if target:
                return target
        except Exception:
            pass
    return real


def _is_owner_or_admin(doc: dict) -> bool:
    """Return True if the current user uploaded `doc` or holds an admin/dev role."""
    u = _current_user()
    return u.get("role") in ("admin", "dev") or doc.get("uploaded_by_id") == u.get("id")


def _agent_has_sharepoint_watch(agent_id: str, watch_id: int) -> bool:
    """
    Return True if `watch_id` is one of the SharePoint connectors attached to
    `agent_id` (via connector_keys in its search_connector_knowledge /
    list_connector_documents tool config). Used to stop a chat upload from
    archiving into a SharePoint site the agent isn't actually wired to.
    """
    import json as _json
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tools_json FROM hub_agents WHERE id = ?", agent_id)
            row = cur.fetchone()
    except Exception:
        return False
    if not row:
        return False
    tools = _json.loads(row[0] or "[]")
    for t in tools:
        if not isinstance(t, dict) or t.get("name") not in (
                "search_connector_knowledge", "list_connector_documents"):
            continue
        for key in (t.get("config") or {}).get("connector_keys") or []:
            if isinstance(key, str) and key == f"sharepoint:{watch_id}":
                return True
    return False


# ── Pages ─────────────────────────────────────────────────────────────────────

@knowledge_bp.route("/knowledge-base")
@_auth.login_required
def knowledge_base_page():
    """Render the Knowledge Base page."""
    return render_template("knowledge_base.html", current_user=_current_user())


@knowledge_bp.route("/connector")
@_auth.login_required
def connector_page():
    """Render the directory/SharePoint connector + dev-proxy page (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        from flask import abort
        abort(403)
    proxy_id   = session.get("proxy_user_id")
    proxy_user = None
    if proxy_id:
        try:
            from org_db import get_proxy_target_user
            proxy_user = get_proxy_target_user(int(proxy_id), int(user.get("id", 0)))
        except Exception:
            pass
    return render_template("connector.html",
                           current_user=user,
                           proxy_user=proxy_user)


# ── Upload ────────────────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/upload", methods=["POST"])
@_auth.login_required
def upload_document():
    """
    Upload a file and ingest it into the knowledge base.

    Form fields: file (required), source_name, client_id, scope, scope_id,
    user_ids (comma-separated or repeated field — explicit per-user
    assignments when scope='user'), destination ('individual' default |
    'sharepoint'), sharepoint_watch_id + agent_id (required together when
    destination='sharepoint').

    When destination='sharepoint', a copy of the original file is uploaded to
    that SharePoint connector's archive folder before indexing, and the
    resulting document is tagged with source_watch_id/source_watch_type so
    it's retrievable via search_connector_knowledge for that connector.

    Returns:
        JSON {success, document_id, ...} (201) or an error (400/403/422/500).
    """
    real_user     = _current_user()
    effective     = _effective_user()

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400

    f           = request.files["file"]
    source_name = (request.form.get("source_name") or f.filename or "").strip()
    client_id   = (request.form.get("client_id") or "").strip()
    scope       = (request.form.get("scope") or "user").strip().lower()
    scope_id_s  = (request.form.get("scope_id") or "").strip()
    scope_id    = int(scope_id_s) if scope_id_s.isdigit() else None

    destination      = (request.form.get("destination") or "individual").strip().lower()
    agent_id         = (request.form.get("agent_id") or "").strip()
    sp_watch_id_s    = (request.form.get("sharepoint_watch_id") or "").strip()
    sharepoint_watch_id = int(sp_watch_id_s) if sp_watch_id_s.isdigit() else None

    if destination == "sharepoint":
        if not agent_id or not sharepoint_watch_id:
            return jsonify({"success": False,
                             "error": "agent_id and sharepoint_watch_id required for destination='sharepoint'"}), 400
        if not _agent_has_sharepoint_watch(agent_id, sharepoint_watch_id):
            return jsonify({"success": False, "error": "SharePoint connector not attached to this agent"}), 403

    # user_ids: comma-separated string or repeated form field for individual assignments
    import json as _json
    raw_uids = request.form.getlist("user_ids") or []
    if len(raw_uids) == 1 and "," in raw_uids[0]:
        raw_uids = [u.strip() for u in raw_uids[0].split(",") if u.strip()]
    assign_user_ids = []
    for uid in raw_uids:
        try:
            assign_user_ids.append(int(uid))
        except (ValueError, TypeError):
            pass

    if scope not in VALID_SCOPES:
        scope = "user"
    if scope in ("department", "project") and not scope_id:
        return jsonify({"success": False, "error": f"scope_id required for scope='{scope}'"}), 400

    if not f.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400

    safe_name = f"{uuid.uuid4().hex[:8]}_{f.filename}"
    save_path = os.path.join(_get_raw_dir(), safe_name)
    try:
        f.save(save_path)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Could not save file: {exc}"}), 500

    uploader_id   = effective.get("id", real_user.get("id", 0))
    uploader_name = effective.get("username", real_user.get("username", ""))

    sharepoint_archive_result = None
    if destination == "sharepoint":
        try:
            from services.sharepoint_watcher import copy_document_to_sharepoint_archive
            ok, err = copy_document_to_sharepoint_archive(sharepoint_watch_id, save_path, f.filename)
            sharepoint_archive_result = {"success": ok, "error": err} if not ok else {"success": True}
        except Exception as exc:
            logger.warning("upload_document: SharePoint archive copy failed: %s", exc)
            sharepoint_archive_result = {"success": False, "error": str(exc)}

    try:
        from agents.core.knowledge.document_processor import process_document
        process_kwargs = dict(
            file_path=save_path,
            client_id=client_id,
            source_name=source_name or f.filename,
            uploaded_by_id=uploader_id,
            uploaded_by_name=uploader_name,
            scope=scope,
            scope_id=scope_id,
        )
        if destination == "sharepoint":
            process_kwargs["source_watch_id"]   = sharepoint_watch_id
            process_kwargs["source_watch_type"] = "sharepoint"
        result = process_document(**process_kwargs)
    except Exception as exc:
        logger.exception("upload_document: processing failed")
        return jsonify({"success": False, "error": str(exc)}), 500

    if sharepoint_archive_result is not None:
        result["sharepoint_archive"] = sharepoint_archive_result

    # Assign to specific users when scope='user' with explicit user_ids
    if result.get("success") and assign_user_ids:
        from agents.core.knowledge.document_processor import assign_document
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                ph  = ",".join("?" for _ in assign_user_ids)
                cur.execute(f"SELECT id, username FROM users WHERE id IN ({ph})",
                            *assign_user_ids)
                umap = {r[0]: r[1] for r in cur.fetchall()}
        except Exception:
            umap = {}
        assigned_names = []
        for uid in assign_user_ids:
            uname = umap.get(uid, str(uid))
            assign_document(result["document_id"], uid, uname, uploader_id, uploader_name)
            assigned_names.append(uname)
        result["assigned_to"] = assigned_names

    return jsonify(result), 201 if result.get("success") else 422


# ── List documents ────────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/documents", methods=["GET"])
@_auth.login_required
def list_documents():
    """
    List documents visible to the current user.

    Query params: client_id, scope (filters client-side after fetch),
    source_watch_type ('filesystem'|'sharepoint'|'upload'), source_watch_id.

    Returns:
        JSON {success, documents, count}.
    """
    user      = _current_user()
    client_id = request.args.get("client_id") or None
    scope_filter = request.args.get("scope") or None
    source_watch_type = request.args.get("source_watch_type") or None
    source_watch_id_s = request.args.get("source_watch_id") or None
    source_watch_id   = int(source_watch_id_s) if source_watch_id_s and source_watch_id_s.isdigit() else None

    from agents.core.knowledge.document_processor import list_documents as _list
    docs = _list(
        user_id=user.get("id", 0),
        role=user.get("role", "user"),
        client_id=client_id,
        source_watch_type=source_watch_type,
        source_watch_id=source_watch_id,
    )
    if scope_filter and scope_filter in VALID_SCOPES:
        docs = [d for d in docs if d.get("scope") == scope_filter]
    for d in docs:
        if d.get("created_at"):
            d["created_at"] = str(d["created_at"])
    return jsonify({"success": True, "documents": docs, "count": len(docs)})


@knowledge_bp.route("/api/knowledge/source-watches", methods=["GET"])
@_auth.login_required
def list_source_watches():
    """Return all directory + SharePoint watches visible to this user (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": True, "filesystem": [], "sharepoint": []})
    try:
        import json as _json
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            if user.get("role") == "admin":
                cur.execute("""
                    SELECT id, folder_path, label, enabled, files_ingested
                    FROM document_watch_dirs ORDER BY label
                """)
                dirs = [{"id": r[0], "folder_path": r[1], "label": r[2] or r[1],
                         "enabled": bool(r[3]), "files_ingested": r[4] or 0}
                        for r in cur.fetchall()]
                cur.execute("""
                    SELECT id, site_url, sp_folder_path, label, enabled, files_ingested
                    FROM sharepoint_watch_configs ORDER BY label
                """)
                sps = [{"id": r[0], "site_url": r[1], "sp_folder_path": r[2],
                        "label": r[3] or r[2] or r[1],
                        "enabled": bool(r[4]), "files_ingested": r[5] or 0}
                       for r in cur.fetchall()]
            else:
                from app_db import get_dev_assigned_resource_ids
                dir_ids = get_dev_assigned_resource_ids(user.get("id", 0), "watch_dir")
                sp_ids  = get_dev_assigned_resource_ids(user.get("id", 0), "sharepoint_watch")
                dirs, sps = [], []
                if dir_ids:
                    ph = ",".join("?" * len(dir_ids))
                    cur.execute(f"""
                        SELECT id, folder_path, label, enabled, files_ingested
                        FROM document_watch_dirs WHERE id IN ({ph}) ORDER BY label
                    """, *dir_ids)
                    dirs = [{"id": r[0], "folder_path": r[1], "label": r[2] or r[1],
                             "enabled": bool(r[3]), "files_ingested": r[4] or 0}
                            for r in cur.fetchall()]
                if sp_ids:
                    ph = ",".join("?" * len(sp_ids))
                    cur.execute(f"""
                        SELECT id, site_url, sp_folder_path, label, enabled, files_ingested
                        FROM sharepoint_watch_configs WHERE id IN ({ph}) ORDER BY label
                    """, *sp_ids)
                    sps = [{"id": r[0], "site_url": r[1], "sp_folder_path": r[2],
                            "label": r[3] or r[2] or r[1],
                            "enabled": bool(r[4]), "files_ingested": r[5] or 0}
                           for r in cur.fetchall()]
        return jsonify({"success": True, "filesystem": dirs, "sharepoint": sps})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Document detail ───────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/documents/<doc_id>", methods=["GET"])
@_auth.login_required
def get_document(doc_id):
    """
    Return a single document's metadata, chunk preview, and assignments.

    Returns:
        JSON {success, document} (200), or 404 if not found/no access.
    """
    user = _current_user()
    from agents.core.knowledge.document_processor import (
        get_document as _get,
        get_document_chunks_preview,
        get_document_assignments,
    )
    doc = _get(doc_id, user_id=user.get("id", 0), role=user.get("role", "user"))
    if not doc:
        return jsonify({"success": False, "error": "Document not found or access denied"}), 404

    doc["created_at"]  = str(doc.get("created_at") or "")
    doc["chunks"]      = get_document_chunks_preview(doc_id, limit=5)
    doc["assignments"] = get_document_assignments(doc_id)
    doc["can_delete"]  = _is_owner_or_admin(doc)
    doc["can_assign"]  = _is_owner_or_admin(doc)
    for a in doc["assignments"]:
        if a.get("assigned_at"):
            a["assigned_at"] = str(a["assigned_at"])
    return jsonify({"success": True, "document": doc})


# ── Delete ────────────────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/documents/<doc_id>", methods=["DELETE"])
@_auth.login_required
def delete_document(doc_id):
    """
    Delete a document (chunks + metadata + assignments + raw file + agent
    tool-config references). Owner or admin/dev only.

    Returns:
        JSON {success, document_id} (200), 404 if not found, 403 if not
        owner/admin.
    """
    user = _current_user()
    from agents.core.knowledge.document_processor import get_document as _get, delete_document as _del
    doc = _get(doc_id, user_id=user.get("id", 0), role=user.get("role", "user"))
    if not doc:
        return jsonify({"success": False, "error": "Not found or access denied"}), 404
    if not _is_owner_or_admin(doc):
        return jsonify({"success": False, "error": "Only the owner or admin can delete"}), 403
    return jsonify(_del(doc_id))


# ── New version ───────────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/documents/<doc_id>/version", methods=["POST"])
@_auth.login_required
def upload_new_version(doc_id):
    """
    Replace an existing document with a new uploaded version.

    Deletes the old document, then ingests the new file with
    version = old_version + 1 and parent_doc_id = doc_id. Owner or
    admin/dev only.

    Returns:
        JSON {success, document_id, previous_doc_id, ...} (201), or an
        error (400/403/404/422/500).
    """
    user = _current_user()
    from agents.core.knowledge.document_processor import (
        get_document as _get,
        delete_document as _del,
        process_document,
    )
    old_doc = _get(doc_id, user_id=user.get("id", 0), role=user.get("role", "user"))
    if not old_doc:
        return jsonify({"success": False, "error": "Not found or access denied"}), 404
    if not _is_owner_or_admin(old_doc):
        return jsonify({"success": False, "error": "Only the owner or admin can replace"}), 403

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400

    f = request.files["file"]
    source_name = (request.form.get("source_name") or old_doc.get("source_name") or f.filename or "").strip()
    client_id   = (request.form.get("client_id") or "").strip()
    scope       = (request.form.get("scope") or old_doc.get("scope") or "user").strip()
    scope_id_s  = (request.form.get("scope_id") or "").strip()
    scope_id    = int(scope_id_s) if scope_id_s.isdigit() else old_doc.get("scope_id")

    safe_name = f"{uuid.uuid4().hex[:8]}_{f.filename}"
    save_path = os.path.join(_get_raw_dir(), safe_name)
    try:
        f.save(save_path)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Could not save file: {exc}"}), 500

    _del(doc_id)

    try:
        result = process_document(
            file_path=save_path,
            client_id=client_id or "",
            source_name=source_name,
            uploaded_by_id=user.get("id", 0),
            uploaded_by_name=user.get("username", ""),
            version=int(old_doc.get("version") or 1) + 1,
            parent_doc_id=doc_id,
            scope=scope,
            scope_id=scope_id,
        )
    except Exception as exc:
        logger.exception("upload_new_version: processing failed")
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({**result, "previous_doc_id": doc_id}), 201 if result.get("success") else 422


# ── Assignments ───────────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/documents/<doc_id>/assignments", methods=["GET"])
@_auth.login_required
def list_assignments(doc_id):
    """
    List the users a document is explicitly assigned to.

    Returns:
        JSON {success, assignments} (200), or 404 if not found/no access.
    """
    user = _current_user()
    from agents.core.knowledge.document_processor import (
        get_document as _get,
        get_document_assignments,
    )
    doc = _get(doc_id, user_id=user.get("id", 0), role=user.get("role", "user"))
    if not doc:
        return jsonify({"success": False, "error": "Not found or access denied"}), 404
    rows = get_document_assignments(doc_id)
    for r in rows:
        if r.get("assigned_at"):
            r["assigned_at"] = str(r["assigned_at"])
    return jsonify({"success": True, "assignments": rows})


@knowledge_bp.route("/api/knowledge/documents/<doc_id>/assign", methods=["POST"])
@_auth.login_required
def assign_document(doc_id):
    """
    Grant one or more users explicit visibility into a document.
    Owner or admin/dev only. Body: {"user_ids": [...]}.

    Returns:
        JSON {success, assigned} (200), or an error (400/403/404).
    """
    user = _current_user()
    from agents.core.knowledge.document_processor import (
        get_document as _get,
        assign_document as _assign,
    )
    doc = _get(doc_id, user_id=user.get("id", 0), role=user.get("role", "user"))
    if not doc:
        return jsonify({"success": False, "error": "Not found or access denied"}), 404
    if not _is_owner_or_admin(doc):
        return jsonify({"success": False, "error": "Only the owner or admin can assign"}), 403

    data     = request.get_json() or {}
    user_ids = data.get("user_ids", [])
    if not user_ids:
        return jsonify({"success": False, "error": "user_ids required"}), 400

    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            placeholders = ",".join(["?" for _ in user_ids])
            cur.execute(f"SELECT id, username FROM users WHERE id IN ({placeholders})",
                        *[int(uid) for uid in user_ids])
            target_users = {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        target_users = {}

    assigned = []
    for uid in user_ids:
        uid   = int(uid)
        uname = target_users.get(uid, str(uid))
        if _assign(doc_id, uid, uname, user.get("id", 0), user.get("username", "")):
            assigned.append({"user_id": uid, "user_name": uname})

    return jsonify({"success": True, "assigned": assigned})


@knowledge_bp.route("/api/knowledge/documents/<doc_id>/assign/<int:target_user_id>", methods=["DELETE"])
@_auth.login_required
def unassign_document(doc_id, target_user_id):
    """
    Revoke a user's explicit assignment to a document. Owner or admin/dev only.

    Returns:
        JSON {success} (200), or an error (403/404).
    """
    user = _current_user()
    from agents.core.knowledge.document_processor import (
        get_document as _get,
        unassign_document as _unassign,
    )
    doc = _get(doc_id, user_id=user.get("id", 0), role=user.get("role", "user"))
    if not doc:
        return jsonify({"success": False, "error": "Not found or access denied"}), 404
    if not _is_owner_or_admin(doc):
        return jsonify({"success": False, "error": "Only the owner or admin can remove assignments"}), 403
    return jsonify({"success": _unassign(doc_id, target_user_id)})


# ── Users list ────────────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/users", methods=["GET"])
@_auth.login_required
def list_users():
    """List all users in the system (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin only"}), 403
    try:
        all_users = _auth.get_all_users()
        rows = [{"id": u["id"], "username": u["username"], "role": u.get("role", "")}
                for u in all_users]
        return jsonify({"success": True, "users": rows})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/colleagues", methods=["GET"])
@_auth.login_required
def list_colleagues():
    """
    Users who can be picked as document-assignment targets.
    Admin/dev see everyone; regular users only see colleagues who share at
    least one department or project with them — not the full directory.
    """
    user = _current_user()
    try:
        if user.get("role") in ("admin", "dev"):
            all_users = _auth.get_all_users()
            rows = [{"id": u["id"], "username": u["username"], "role": u.get("role", "")}
                    for u in all_users]
        else:
            import org_db
            rows = org_db.get_colleagues(user.get("id", 0))
        return jsonify({"success": True, "users": rows})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/my-departments", methods=["GET"])
@_auth.login_required
def list_my_departments():
    """Departments the current user belongs to — any logged-in user, not just admin/dev."""
    user = _current_user()
    try:
        import org_db
        depts = org_db.get_my_departments(user.get("id", 0))
        return jsonify({"success": True, "departments": depts})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/departments", methods=["GET"])
@_auth.login_required
def list_departments():
    """List all departments in the org (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin only"}), 403
    try:
        import org_db
        depts = org_db.get_all_departments()
        return jsonify({"success": True, "departments": depts})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/projects", methods=["GET"])
@_auth.login_required
def list_projects():
    """List all projects in the org (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin only"}), 403
    try:
        import org_db
        projects = org_db.get_all_projects()
        return jsonify({"success": True, "projects": projects})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Hybrid RAG search ─────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/search", methods=["POST"])
@_auth.login_required
def search_knowledge():
    """
    Run an ad-hoc hybrid RAG search over the documents visible to the
    current user.

    Body: {"query": str (required), "n_results": int (default 8, max 20),
    "session_id": str (optional, falls back to the Flask session id)}.

    Returns:
        JSON {success, query, count, results, citations, confidence,
        fallback, from_cache, latency_ms} (200), or 400 if query is missing.
    """
    user  = _current_user()
    data  = request.get_json() or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"success": False, "error": "query required"}), 400

    n           = min(int(data.get("n_results", 8)), 20)
    session_id  = data.get("session_id") or session.get("session_id", "")
    role        = user.get("role", "user")
    uid         = user.get("id", 0)

    from agents.core.knowledge.document_processor import get_visible_doc_ids
    from agents.core.knowledge.rag_pipeline import get_pipeline

    visible_ids = get_visible_doc_ids(uid, role)
    doc_ids_set = set(visible_ids) if visible_ids is not None else None

    pipeline = get_pipeline()
    result   = pipeline.run(
        query=query,
        doc_ids=doc_ids_set,
        n_results=n,
        user_id=uid,
        session_id=session_id,
    )

    # Shape results for the client
    formatted = []
    for r in result.get("results", []):
        formatted.append({
            "text":        r.get("text_content") or r.get("text", ""),
            "document_id": r.get("document_id", ""),
            "filename":    r.get("filename", ""),
            "source_name": r.get("source_name", ""),
            "score":       r.get("score", 0),
            "confidence":  r.get("confidence", 0),
            "metadata":    {"file_type": r.get("file_type", ""),
                            "chunk_index": r.get("chunk_index")},
        })

    return jsonify({
        "success":    True,
        "query":      query,
        "count":      len(formatted),
        "results":    formatted,
        "citations":  result.get("citations", []),
        "confidence": result.get("confidence", 0),
        "fallback":   result.get("fallback", False),
        "from_cache": result.get("from_cache", False),
        "latency_ms": result.get("latency_ms", 0),
    })


# ── Extract text only ─────────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/extract-text", methods=["POST"])
@_auth.login_required
def extract_text():
    """
    Extract and return a file's text content without indexing it into the
    knowledge base. Truncates the returned text to 8000 chars.

    Returns:
        JSON {success, filename, file_type, text, truncated, full_length,
        server_path} (200), or an error (400/422/500).
    """
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400

    safe_name = f"{uuid.uuid4().hex[:8]}_{f.filename}"
    save_path = os.path.join(_get_raw_dir(), safe_name)
    try:
        f.save(save_path)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Could not save file: {exc}"}), 500

    try:
        ext = os.path.splitext(f.filename)[1].lower()
        from agents.core.knowledge.document_processor import _EXTRACTOR_MAP
        extractor = _EXTRACTOR_MAP.get(ext)
        if not extractor:
            return jsonify({
                "success": False,
                "error":   f"Unsupported file type '{ext}' for inline reading.",
                "filename": f.filename,
            }), 422
        text, meta = extractor(save_path)
        if not text or not text.strip():
            return jsonify({
                "success":  False,
                "error":    meta.get("error") or "No text could be extracted.",
                "filename": f.filename,
            }), 422
        truncated = len(text) > 8000
        return jsonify({
            "success":     True,
            "filename":    f.filename,
            "file_type":   ext.lstrip("."),
            "text":        text[:8000],
            "truncated":   truncated,
            "full_length": len(text),
            "server_path": save_path,
        })
    except Exception as exc:
        logger.exception("extract_text failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# DIRECTORY WATCHER / CONNECTOR  — admin + dev
# ══════════════════════════════════════════════════════════════════════════════

@knowledge_bp.route("/api/knowledge/watch-dirs", methods=["GET"])
@_auth.login_required
def list_watch_dirs():
    """
    List filesystem watch directories. Admin sees all; dev sees only those
    explicitly assigned to them.

    Returns:
        JSON {success, watch_dirs} (200), or 500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            if user.get("role") == "admin":
                cur.execute("""
                    SELECT id, folder_path, label, scope, scope_id,
                           target_user_id, created_by, created_at,
                           last_scanned, enabled, files_ingested, key_metrics
                    FROM document_watch_dirs
                    ORDER BY created_at DESC
                """)
            else:
                # Dev sees only dirs explicitly assigned to them by an admin
                from app_db import get_dev_assigned_resource_ids
                assigned_ids = get_dev_assigned_resource_ids(user.get("id", 0), "watch_dir")
                if assigned_ids:
                    placeholders = ",".join("?" * len(assigned_ids))
                    cur.execute(f"""
                        SELECT id, folder_path, label, scope, scope_id,
                               target_user_id, created_by, created_at,
                               last_scanned, enabled, files_ingested, key_metrics
                        FROM document_watch_dirs
                        WHERE id IN ({placeholders})
                        ORDER BY created_at DESC
                    """, *assigned_ids)
                else:
                    cur.execute("""
                        SELECT id, folder_path, label, scope, scope_id,
                               target_user_id, created_by, created_at,
                               last_scanned, enabled, files_ingested, key_metrics
                        FROM document_watch_dirs
                        WHERE 1=0
                    """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            for k in ("created_at", "last_scanned"):
                if r.get(k):
                    r[k] = str(r[k])
            import json as _json
            km = r.get("key_metrics")
            r["key_metrics"] = _json.loads(km) if km else []
        return jsonify({"success": True, "watch_dirs": rows})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/watch-dirs", methods=["POST"])
@_auth.login_required
def add_watch_dir():
    """
    Register a new filesystem directory to watch for auto-ingestion.

    Body: {"folder_path" (required), "label", "scope", "scope_id",
    "target_user_id", "user_ids", "key_metrics"}. Dev role may only target
    users they're authorized to proxy as.

    Returns:
        JSON {success, id, folder_path} (200), or an error (400/403/500).
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403

    import json as _json
    data        = request.get_json() or {}
    fpath       = (data.get("folder_path") or "").strip()
    label       = (data.get("label") or fpath).strip()
    scope       = (data.get("scope") or "user").strip().lower()
    sid         = data.get("scope_id")
    target      = data.get("target_user_id")
    user_ids    = [int(x) for x in (data.get("user_ids") or []) if str(x).isdigit()]
    key_metrics = data.get("key_metrics") or []
    if isinstance(key_metrics, str):
        key_metrics = [m.strip() for m in key_metrics.split(",") if m.strip()]
    key_metrics_json  = _json.dumps(key_metrics) if key_metrics else None
    user_ids_json_str = _json.dumps(user_ids)    if user_ids    else None

    if not fpath:
        return jsonify({"success": False, "error": "folder_path required"}), 400
    if not os.path.isdir(fpath):
        return jsonify({"success": False,
                        "error": f"Folder does not exist: {fpath}"}), 400
    if scope not in VALID_SCOPES:
        scope = "user"

    # Dev role: target_user_id must be one of their proxy targets
    if user.get("role") == "dev" and target:
        from org_db import get_dev_proxy_users
        allowed = [u["id"] for u in get_dev_proxy_users(user.get("id", 0))]
        if int(target) not in allowed:
            return jsonify({"success": False,
                            "error": "Not authorized to create resources for that user"}), 403

    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                MERGE document_watch_dirs AS t
                USING (SELECT ? AS folder_path, ? AS target_user_id) AS s
                      ON t.folder_path = s.folder_path
                         AND (t.target_user_id = s.target_user_id
                              OR (t.target_user_id IS NULL AND s.target_user_id IS NULL))
                WHEN NOT MATCHED THEN
                    INSERT (folder_path, label, scope, scope_id, target_user_id,
                            created_by, key_metrics, user_ids_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                OUTPUT INSERTED.id;
            """, fpath, target,
                fpath, label, scope, sid, target, user.get("id", 0),
                key_metrics_json, user_ids_json_str)
            row = cur.fetchone()
            conn.commit()
            new_id = row[0] if row else None

        # Register with watcher service
        try:
            from services.document_watcher import get_watcher
            get_watcher().add_watch(new_id, fpath, label, scope,
                                    scope_id=sid, target_user_id=target,
                                    key_metrics=key_metrics, user_ids=user_ids)
        except Exception as we:
            logger.warning("watch-dirs: could not register with watcher service: %s", we)

        return jsonify({"success": True, "id": new_id, "folder_path": fpath})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/watch-dirs/<int:dir_id>", methods=["PUT"])
@_auth.login_required
def update_watch_dir(dir_id):
    """
    Update a watch directory's enabled flag and/or label.
    Body: {"enabled": bool, "label": str} (admin/dev only).

    Returns:
        JSON {success} (200), or an error (403/500).
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    data    = request.get_json() or {}
    enabled = data.get("enabled")
    label   = data.get("label")
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            parts, params = [], []
            if enabled is not None:
                parts.append("enabled = ?")
                params.append(1 if enabled else 0)
            if label is not None:
                parts.append("label = ?")
                params.append(str(label))
            if parts:
                params.append(dir_id)
                cur.execute(f"UPDATE document_watch_dirs SET {', '.join(parts)} WHERE id = ?",
                            *params)
                conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/watch-dirs/<int:dir_id>", methods=["DELETE"])
@_auth.login_required
def delete_watch_dir(dir_id):
    """
    Remove a watch directory's DB row and stop watching it at runtime
    (admin/dev only).

    Returns:
        JSON {success} (200), or 403/500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT folder_path FROM document_watch_dirs WHERE id = ?", dir_id)
            row = cur.fetchone()
            fpath = row[0] if row else None
            cur.execute("DELETE FROM document_watch_dirs WHERE id = ?", dir_id)
            conn.commit()

        if fpath:
            try:
                from services.document_watcher import get_watcher
                get_watcher().remove_watch(fpath)
            except Exception:
                pass

        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/watch-dirs/<int:dir_id>/scan", methods=["POST"])
@_auth.login_required
def scan_watch_dir(dir_id):
    """
    Trigger an immediate ingestion scan of a watch directory's current
    contents (admin/dev only).

    Returns:
        JSON {success, files_ingested} (200), or an error (403/404/500).
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT folder_path, scope, scope_id, target_user_id
                FROM document_watch_dirs WHERE id = ?
            """, dir_id)
            row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Watch dir not found"}), 404

        folder_path, scope, scope_id, target_user_id = row
        from services.document_watcher import get_watcher
        ingested = get_watcher().scan_now(
            folder_path, scope, scope_id, target_user_id,
            triggered_by_id=user.get("id", 0),
            triggered_by_name=user.get("username", ""),
        )
        return jsonify({"success": True, "files_ingested": ingested})
    except Exception as exc:
        logger.exception("scan_watch_dir failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# DEV PROXY
# ══════════════════════════════════════════════════════════════════════════════

@knowledge_bp.route("/api/dev/proxy/targets", methods=["GET"])
@_auth.login_required
def proxy_targets():
    """Returns the users this dev/admin can connect as."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from org_db import get_dev_proxy_users
        targets = get_dev_proxy_users(user.get("id", 0), is_admin=user.get("role") == "admin")
        return jsonify({"success": True, "targets": targets,
                        "active_proxy_id": session.get("proxy_user_id")})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/dev/proxy/connect", methods=["POST"])
@_auth.login_required
def proxy_connect():
    """
    Connect as a proxy target user, storing the target's ID in the session.
    Body: {"target_user_id": int} (admin/dev only).

    Returns:
        JSON {success, connected_as} (200), or an error (400/403).
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403

    data      = request.get_json() or {}
    target_id = data.get("target_user_id")
    if not target_id:
        return jsonify({"success": False, "error": "target_user_id required"}), 400

    try:
        from org_db import get_proxy_target_user
        target = get_proxy_target_user(int(target_id), int(user.get("id", 0)))
        if not target:
            return jsonify({"success": False, "error": "Not authorized or user not found"}), 403
        session["proxy_user_id"] = int(target_id)
        return jsonify({
            "success": True,
            "connected_as": {"id": target["id"], "username": target["username"]},
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/dev/proxy/disconnect", methods=["POST"])
@_auth.login_required
def proxy_disconnect():
    """Clear the active proxy target from the session."""
    session.pop("proxy_user_id", None)
    return jsonify({"success": True})


# ── Admin: manage proxy assignments ──────────────────────────────────────────

@knowledge_bp.route("/api/admin/proxy-assignments", methods=["GET"])
@_auth.login_required
def get_proxy_assignments():
    """List all dev-to-target-user proxy assignments (admin only)."""
    user = _current_user()
    if user.get("role") != "admin":
        return jsonify({"success": False, "error": "Admin only"}), 403
    try:
        from org_db import list_all_proxy_assignments
        rows = list_all_proxy_assignments()
        return jsonify({"success": True, "assignments": rows})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/admin/proxy-assignments", methods=["POST"])
@_auth.login_required
def set_proxy_assignments():
    """Set the target users a dev can proxy as. Replaces existing."""
    user = _current_user()
    if user.get("role") != "admin":
        return jsonify({"success": False, "error": "Admin only"}), 403
    data        = request.get_json() or {}
    dev_user_id = data.get("dev_user_id")
    target_ids  = data.get("target_user_ids", [])
    if not dev_user_id:
        return jsonify({"success": False, "error": "dev_user_id required"}), 400
    try:
        from org_db import set_dev_proxy_assignments
        set_dev_proxy_assignments(int(dev_user_id), target_ids, assigned_by=user.get("id", 0))
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# SHAREPOINT CONNECTOR  — admin + dev
# ══════════════════════════════════════════════════════════════════════════════

@knowledge_bp.route("/api/knowledge/sharepoint-watches", methods=["GET"])
@_auth.login_required
def list_sharepoint_watches():
    """
    List SharePoint folder watch configs. Admin sees all; dev sees only
    those explicitly assigned to them.

    Returns:
        JSON {success, watches} (200), or 403/500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            if user.get("role") == "admin":
                cur.execute("""
                    SELECT id, site_url, sp_folder_path, label, scope, scope_id,
                           target_user_id, created_by, created_at,
                           last_scanned, enabled, files_ingested, poll_interval, key_metrics
                    FROM sharepoint_watch_configs
                    ORDER BY created_at DESC
                """)
            else:
                # Dev sees only SharePoint configs explicitly assigned to them by an admin
                from app_db import get_dev_assigned_resource_ids
                assigned_ids = get_dev_assigned_resource_ids(user.get("id", 0), "sharepoint_watch")
                if assigned_ids:
                    placeholders = ",".join("?" * len(assigned_ids))
                    cur.execute(f"""
                        SELECT id, site_url, sp_folder_path, label, scope, scope_id,
                               target_user_id, created_by, created_at,
                               last_scanned, enabled, files_ingested, poll_interval, key_metrics
                        FROM sharepoint_watch_configs
                        WHERE id IN ({placeholders})
                        ORDER BY created_at DESC
                    """, *assigned_ids)
                else:
                    cur.execute("""
                        SELECT id, site_url, sp_folder_path, label, scope, scope_id,
                               target_user_id, created_by, created_at,
                               last_scanned, enabled, files_ingested, poll_interval, key_metrics
                        FROM sharepoint_watch_configs
                        WHERE 1=0
                    """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        import json as _json
        for r in rows:
            for k in ("created_at", "last_scanned"):
                if r.get(k):
                    r[k] = str(r[k])
            km = r.get("key_metrics")
            r["key_metrics"] = _json.loads(km) if km else []
        return jsonify({"success": True, "watches": rows})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/sharepoint-watches", methods=["POST"])
@_auth.login_required
def add_sharepoint_watch():
    """
    Register a new SharePoint folder to watch for auto-ingestion.

    Body: {"site_url" (required), "sp_folder_path", "label", "scope",
    "scope_id", "target_user_id", "user_ids", "poll_interval" (clamped to
    30-86400s), "key_metrics"} (admin/dev only).

    Returns:
        JSON {success, id} (200), or an error (400/403/500).
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403

    import json as _json
    data          = request.get_json() or {}
    site_url      = (data.get("site_url") or "").strip()
    sp_folder     = (data.get("sp_folder_path") or "").strip()
    label         = (data.get("label") or sp_folder or site_url).strip()
    scope         = (data.get("scope") or "user").strip().lower()
    scope_id      = data.get("scope_id")
    target        = data.get("target_user_id")
    user_ids      = [int(x) for x in (data.get("user_ids") or []) if str(x).isdigit()]
    poll_interval = int(data.get("poll_interval") or 60)
    key_metrics   = data.get("key_metrics") or []
    if isinstance(key_metrics, str):
        key_metrics = [m.strip() for m in key_metrics.split(",") if m.strip()]
    key_metrics_json  = _json.dumps(key_metrics) if key_metrics else None
    user_ids_json_str = _json.dumps(user_ids)    if user_ids    else None

    if not site_url:
        return jsonify({"success": False, "error": "site_url required"}), 400
    if scope not in VALID_SCOPES:
        scope = "user"

    poll_interval = max(30, min(poll_interval, 86400))

    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sharepoint_watch_configs
                    (site_url, sp_folder_path, label, scope, scope_id,
                     target_user_id, created_by, poll_interval, key_metrics, user_ids_json)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, site_url, sp_folder, label, scope, scope_id,
                target, user.get("id", 0), poll_interval, key_metrics_json, user_ids_json_str)
            row    = cur.fetchone()
            conn.commit()
            new_id = row[0] if row else None

        try:
            from services.sharepoint_watcher import get_sp_watcher
            get_sp_watcher().add_watch(
                new_id, site_url, sp_folder,
                label=label, scope=scope, scope_id=scope_id,
                target_user_id=target, poll_interval=poll_interval,
                key_metrics=key_metrics, user_ids=user_ids,
            )
        except Exception as we:
            logger.warning("sharepoint-watches: could not register with watcher: %s", we)

        return jsonify({"success": True, "id": new_id})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/sharepoint-watches/<int:watch_id>", methods=["PUT"])
@_auth.login_required
def update_sharepoint_watch(watch_id):
    """
    Update a SharePoint watch's enabled flag, label, and/or poll_interval
    (clamped to 30-86400s). Admin/dev only.

    Returns:
        JSON {success} (200), or 403/500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    data    = request.get_json() or {}
    enabled = data.get("enabled")
    label   = data.get("label")
    poll    = data.get("poll_interval")
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur    = conn.cursor()
            parts, params = [], []
            if enabled is not None:
                parts.append("enabled = ?")
                params.append(1 if enabled else 0)
            if label is not None:
                parts.append("label = ?")
                params.append(str(label))
            if poll is not None:
                parts.append("poll_interval = ?")
                params.append(max(30, min(int(poll), 86400)))
            if parts:
                params.append(watch_id)
                cur.execute(f"UPDATE sharepoint_watch_configs SET {', '.join(parts)} WHERE id = ?",
                            *params)
                conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/sharepoint-watches/<int:watch_id>", methods=["DELETE"])
@_auth.login_required
def delete_sharepoint_watch(watch_id):
    """
    Remove a SharePoint watch's DB row and stop polling it at runtime
    (admin/dev only).

    Returns:
        JSON {success} (200), or 403/500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM sharepoint_watch_configs WHERE id = ?", watch_id)
            conn.commit()
        try:
            from services.sharepoint_watcher import get_sp_watcher
            get_sp_watcher().remove_watch(watch_id)
        except Exception:
            pass
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@knowledge_bp.route("/api/knowledge/sharepoint-watches/<int:watch_id>/scan", methods=["POST"])
@_auth.login_required
def scan_sharepoint_watch(watch_id):
    """
    Trigger an immediate poll of a SharePoint watch (admin/dev only).

    Returns:
        JSON {success, files_ingested} (200), or 403/500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    try:
        from services.sharepoint_watcher import get_sp_watcher
        ingested = get_sp_watcher().scan_now(watch_id)
        return jsonify({"success": True, "files_ingested": ingested})
    except Exception as exc:
        logger.exception("scan_sharepoint_watch failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Observability: retrieval traces ──────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/rag-traces", methods=["GET"])
@_auth.login_required
def rag_traces():
    """
    Return recent retrieval trace log rows (admin/dev only).
    Query param: limit (default 50, max 200).

    Returns:
        JSON {success, traces, count} (200), or 403/500 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    limit = min(int(request.args.get("limit", 50)), 200)
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT TOP (?) id, session_id, user_id, query, method,
                               result_count, confidence, fallback, latency_ms, created_at
                FROM app_retrieval_traces
                ORDER BY created_at DESC
            """, limit)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
        return jsonify({"success": True, "traces": rows, "count": len(rows)})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ── RAG aggregate stats ───────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/rag-stats", methods=["GET"])
@_auth.login_required
def rag_stats():
    """
    Return aggregate RAG retrieval metrics (admin/dev only).
    Query param: days (default 7, max 90).

    Returns:
        JSON {success, period_days, summary, top_queries, latency_buckets}
        (200), or 403 if not admin/dev.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    days = min(int(request.args.get("days", 7)), 90)
    from agents.core.knowledge.rag_pipeline import get_rag_stats
    return jsonify({"success": True, **get_rag_stats(days)})


# ── RAG observability dashboard page ─────────────────────────────────────────

@knowledge_bp.route("/rag-dashboard", methods=["GET"])
@_auth.login_required
def rag_dashboard():
    """Render the RAG observability dashboard page (admin only)."""
    user = _current_user()
    if user.get("role") != "admin":
        return jsonify({"success": False, "error": "Admin only"}), 403
    return render_template("rag_dashboard.html", user=user)


# ── Evaluation: test query CRUD ───────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/eval-queries", methods=["GET"])
@_auth.login_required
def get_eval_queries():
    """List all registered RAG evaluation test queries (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    from agents.core.knowledge.rag_pipeline import list_eval_queries
    return jsonify({"success": True, "queries": list_eval_queries()})


@knowledge_bp.route("/api/knowledge/eval-queries", methods=["POST"])
@_auth.login_required
def create_eval_query():
    """
    Register a new evaluation test query.
    Body: {"query" (required), "expected_doc_ids", "category"}
    (admin/dev only).

    Returns:
        JSON {success, id} (200), or 400/403 on error.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    data = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"success": False, "error": "query is required"}), 400
    from agents.core.knowledge.rag_pipeline import add_eval_query
    qid = add_eval_query(
        query=query,
        expected_doc_ids=data.get("expected_doc_ids") or [],
        category=data.get("category", "recall"),
    )
    return jsonify({"success": True, "id": qid})


@knowledge_bp.route("/api/knowledge/eval-queries/<int:qid>", methods=["DELETE"])
@_auth.login_required
def remove_eval_query(qid):
    """Delete a registered evaluation test query by ID (admin/dev only)."""
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    from agents.core.knowledge.rag_pipeline import delete_eval_query
    return jsonify({"success": delete_eval_query(qid)})


# ── Evaluation: run + results ─────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/eval-run", methods=["POST"])
@_auth.login_required
def run_eval():
    """
    Run all registered evaluation queries through the RAG pipeline,
    scoped to the current user's visible documents (admin/dev only).

    Returns:
        JSON {success, total_queries, recall_at_1/3/5, fallback_rate,
        avg_latency_ms} (200), or 403 if not admin/dev.
    """
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    from agents.core.knowledge.rag_pipeline import run_evaluation
    from agents.core.knowledge.document_processor import get_visible_doc_ids
    doc_ids = get_visible_doc_ids(user.get("id", 0), user.get("role", "user"))
    result = run_evaluation(
        user_id=user.get("id", 0),
        doc_ids=set(doc_ids) if doc_ids else None,
    )
    return jsonify(result)


@knowledge_bp.route("/api/knowledge/eval-results", methods=["GET"])
@_auth.login_required
def eval_results():
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403
    limit = min(int(request.args.get("limit", 50)), 200)
    from agents.core.knowledge.rag_pipeline import get_eval_results
    return jsonify({"success": True, "results": get_eval_results(limit)})


# ── Connector monitor logs ────────────────────────────────────────────────────

@knowledge_bp.route("/api/knowledge/connector-logs", methods=["GET"])
@_auth.login_required
def connector_logs():
    user = _current_user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"success": False, "error": "Admin/dev only"}), 403

    limit      = min(int(request.args.get("limit", 100)), 500)
    watch_type = request.args.get("watch_type", "").strip()   # 'filesystem' | 'sharepoint' | ''
    watch_id   = request.args.get("watch_id", "").strip()
    status     = request.args.get("status", "").strip()       # 'success' | 'error' | ''

    conditions = []
    params     = []
    if watch_type:
        conditions.append("watch_type = ?")
        params.append(watch_type)
    if watch_id.isdigit():
        conditions.append("watch_id = ?")
        params.append(int(watch_id))
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    try:
        from database.app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT TOP (?) id, watch_type, watch_id, watch_label,
                               filename, status, chunk_count, error_msg, created_at
                FROM connector_logs
                {where}
                ORDER BY created_at DESC
            """, [limit] + params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = str(r["created_at"])
        return jsonify({"success": True, "logs": rows, "count": len(rows)})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500
