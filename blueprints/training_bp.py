"""
training_bp.py — Flask Blueprint for the SLM Training Data module.

Serves the review UI and JSON/file API for chat-derived training pairs
captured by `training_db` (Workspace chat instruction/output pairs). For the
BI-tool equivalent (query/dashboard/report/infographic/presentation/optimize
pairs from `bi_training_db`), see whichever blueprint wires up the BI
training review UI — this module covers chat pairs only.

Routes
------
  GET  /workspace/training                          — main review UI page
  POST /api/workspace/training/extract               — auto-extract pairs from conversation(s)
  GET  /api/workspace/training/pairs                  — paginated pair list (JSON)
  POST /api/workspace/training/pairs/<id>/review      — approve / reject / tag one pair
  POST /api/workspace/training/pairs/bulk-review      — approve / reject up to 500 pairs at once
  DELETE /api/workspace/training/pairs/<id>           — delete a pair
  GET  /api/workspace/training/stats                  — KPI stats
  GET  /api/workspace/training/export                 — download a JSONL export file
  GET  /api/workspace/training/export-history          — past exports
  GET  /api/workspace/training/preference-pairs        — paginated DPO preference-pair list (JSON)
"""

import hashlib
import io
import json
from logging_config import get_logger

from flask import (
    Blueprint, Response, jsonify, render_template, request, session, stream_with_context
)

import auth
import token_limits
import training_db
import workspace_db

logger = get_logger(__name__)
training_bp = Blueprint("training", __name__)


def _current_user():
    """Return the current session's user dict, or None if not logged in."""
    return auth.current_user()


def _require_login():
    """Return ``(user, None)`` if logged in, else ``(None, (401 response, ...))``."""
    user = _current_user()
    if not user:
        return None, (jsonify({"error": "Unauthorized"}), 401)
    return user, None


def _require_admin_or_dev():
    """Return ``(user, None)`` if logged in as admin/dev, else ``(None, error_response)``.

    The error response is 401 (Unauthorized) if not logged in at all, or 403
    (Forbidden) if logged in with an insufficient role.
    """
    user, err = _require_login()
    if err:
        return None, err
    if user.get("role") not in ("admin", "dev"):
        return None, (jsonify({"error": "Forbidden"}), 403)
    return user, None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@training_bp.route("/workspace/training")
def training_page():
    """Render the SLM training review page, pre-loaded with stats and recent export history.

    Redirects to login if no user is in session.
    """
    user = _current_user()
    if not user:
        from flask import redirect, url_for
        return redirect(url_for("login"))

    stats = training_db.get_training_stats()
    export_history = training_db.get_export_history(10)
    return render_template(
        "workspace/training.html",
        user=user,
        stats=stats,
        export_history=export_history,
    )


# ---------------------------------------------------------------------------
# Auto-extract
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/extract", methods=["POST"])
def api_extract():
    """Auto-extract training pairs from one conversation, or all conversations (admin/dev only).

    JSON body:
      - ``conversation_id`` (int): extract pairs from this conversation only.
      - ``workspace_id`` (int, optional): scope ``extract_all`` to one workspace.
      - ``extract_all`` (bool): if true, ignore ``conversation_id`` and process
        every conversation (optionally filtered by ``workspace_id``).

    Returns:
        ``{"inserted": N}`` for a single conversation, or
        ``{"inserted": N, "conversations_processed": M}`` for ``extract_all``.
    """
    user, err = _require_admin_or_dev()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    workspace_id    = data.get("workspace_id")
    extract_all     = data.get("extract_all", False)

    if extract_all:
        # Pull all conversations for this workspace (or all if no workspace_id)
        try:
            with workspace_db._get_db() as conn:
                cursor = conn.cursor()
                if workspace_id:
                    cursor.execute(
                        "SELECT id FROM ws_conversations WHERE workspace_id = ?",
                        workspace_id,
                    )
                else:
                    cursor.execute("SELECT id FROM ws_conversations")
                conv_ids = [r[0] for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"training extract_all query failed: {e}")
            return jsonify({"error": str(e)}), 500

        total = 0
        for cid in conv_ids:
            try:
                total += training_db.auto_extract_and_insert(
                    cid, workspace_id=workspace_id
                )
            except Exception as e:
                logger.warning(f"extract failed for conv {cid}: {e}")
        return jsonify({"inserted": total, "conversations_processed": len(conv_ids)})

    if not conversation_id:
        return jsonify({"error": "conversation_id required"}), 400

    try:
        inserted = training_db.auto_extract_and_insert(
            int(conversation_id), workspace_id=workspace_id
        )
        return jsonify({"inserted": inserted})
    except Exception as e:
        logger.error(f"training extract failed: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Pair list
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/pairs")
def api_pairs():
    """List training pairs for the review grid, filtered and paginated via query string.

    Query params: ``status``, ``domain``, ``workspace_id``, ``min_score``,
    ``max_score``, ``page``, ``per_page`` (capped at 200).

    Returns:
        ``{"pairs": [...], "total": N, "page": P, "per_page": PP}``.
    """
    user, err = _require_login()
    if err:
        return err

    status      = request.args.get("status", "")
    domain      = request.args.get("domain", "")
    workspace_id= request.args.get("workspace_id", type=int)
    min_score   = request.args.get("min_score", 0.0, type=float)
    max_score   = request.args.get("max_score", 100.0, type=float)
    page        = request.args.get("page", 1, type=int)
    per_page    = min(request.args.get("per_page", 50, type=int), 200)

    try:
        rows, total = training_db.get_pairs(
            status=status,
            domain=domain,
            workspace_id=workspace_id,
            min_score=min_score,
            max_score=max_score,
            page=page,
            per_page=per_page,
        )
        return jsonify({"pairs": rows, "total": total, "page": page, "per_page": per_page})
    except Exception as e:
        logger.error(f"api_pairs failed: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Review: approve / reject / tag
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/pairs/<int:pair_id>/review", methods=["POST"])
def api_review_pair(pair_id: int):
    """Approve, reject, or re-tag a single training pair (admin/dev only).

    JSON body: ``action`` (``"approve"`` | ``"reject"`` | ``"tag"``), plus
    ``notes`` (for approve/reject) or ``domain``/``tags`` (for tag).

    Returns:
        ``{"ok": True, "pair": <updated pair dict>}``, or 400 if ``action``
        is not one of the three supported values.
    """
    user, err = _require_admin_or_dev()
    if err:
        return err

    data   = request.get_json(silent=True) or {}
    action = data.get("action", "")   # approve | reject | tag
    notes  = data.get("notes", "")
    domain = data.get("domain", "")
    tags   = data.get("tags", "")

    if action in ("approve", "reject"):
        status_map = {"approve": "approved", "reject": "rejected"}
        training_db.update_pair_status(
            pair_id, status_map[action], user["id"], notes
        )
    elif action == "tag":
        training_db.update_pair_domain_tags(pair_id, domain, tags)
    else:
        return jsonify({"error": "Invalid action"}), 400

    pair = training_db.get_pair_by_id(pair_id)
    return jsonify({"ok": True, "pair": pair})


# ---------------------------------------------------------------------------
# Bulk review
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/pairs/bulk-review", methods=["POST"])
def api_bulk_review():
    """Approve or reject up to 500 training pairs in one request (admin/dev only).

    JSON body: ``ids`` (list of pair ids, capped at 500), ``action``
    (``"approve"`` | ``"reject"``), and optional ``notes`` applied to every pair.

    Individual failures are logged and skipped rather than aborting the
    whole batch.

    Returns:
        ``{"ok": True, "updated": N}`` where N is the count that succeeded.
    """
    user, err = _require_admin_or_dev()
    if err:
        return err

    data    = request.get_json(silent=True) or {}
    ids     = data.get("ids", [])
    action  = data.get("action", "")
    notes   = data.get("notes", "")

    if action not in ("approve", "reject"):
        return jsonify({"error": "action must be approve or reject"}), 400
    if not ids:
        return jsonify({"error": "ids list required"}), 400

    status_map = {"approve": "approved", "reject": "rejected"}
    updated = 0
    for pid in ids[:500]:  # cap at 500
        try:
            training_db.update_pair_status(int(pid), status_map[action], user["id"], notes)
            updated += 1
        except Exception as e:
            logger.warning(f"bulk review failed for pair {pid}: {e}")

    return jsonify({"ok": True, "updated": updated})


# ---------------------------------------------------------------------------
# Delete pair
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/pairs/<int:pair_id>", methods=["DELETE"])
def api_delete_pair(pair_id: int):
    """Permanently delete a training pair and its annotations (admin/dev only)."""
    user, err = _require_admin_or_dev()
    if err:
        return err
    training_db.delete_pair(pair_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/stats")
def api_stats():
    """Return KPI stats for the training dashboard (any logged-in user)."""
    user, err = _require_login()
    if err:
        return err
    stats = training_db.get_training_stats()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

EXPORT_FORMATS = {"alpaca_jsonl", "chatml_jsonl", "openai_ft_jsonl", "dpo_jsonl"}


def _build_alpaca(pairs: list[dict]) -> list[str]:
    """Render pairs as Alpaca-format JSONL lines (``instruction``/``input``/``output``).

    Args:
        pairs: Rows from `training_db.get_approved_pairs_for_export`.

    Returns:
        One JSON string per pair, ready to be newline-joined.
    """
    lines = []
    for p in pairs:
        obj = {
            "instruction": p["instruction"],
            "input": p.get("context") or "",
            "output": p["output"],
        }
        if p.get("domain"):
            obj["domain"] = p["domain"]
        lines.append(json.dumps(obj, ensure_ascii=False))
    return lines


def _build_chatml(pairs: list[dict]) -> list[str]:
    """Render pairs as ChatML-style JSONL lines (``{"messages": [...]}`` per line).

    Args:
        pairs: Rows from `training_db.get_approved_pairs_for_export`.

    Returns:
        One JSON string per pair, ready to be newline-joined.
    """
    lines = []
    for p in pairs:
        messages = []
        if p.get("context"):
            messages.append({"role": "system", "content": p["context"]})
        messages.append({"role": "user", "content": p["instruction"]})
        messages.append({"role": "assistant", "content": p["output"]})
        lines.append(json.dumps({"messages": messages}, ensure_ascii=False))
    return lines


def _build_openai_ft(pairs: list[dict]) -> list[str]:
    """Render pairs as OpenAI fine-tuning JSONL lines.

    Currently produces output identical in shape to `_build_chatml` (both use
    the ``{"messages": [...]}`` schema) — kept as a separate format so the
    export UI can offer it under its own label even though the payload shape
    happens to match today.

    Args:
        pairs: Rows from `training_db.get_approved_pairs_for_export`.

    Returns:
        One JSON string per pair, ready to be newline-joined.
    """
    lines = []
    for p in pairs:
        messages = []
        if p.get("context"):
            messages.append({"role": "system", "content": p["context"]})
        messages.append({"role": "user", "content": p["instruction"]})
        messages.append({"role": "assistant", "content": p["output"]})
        lines.append(json.dumps({"messages": messages}, ensure_ascii=False))
    return lines


def _build_dpo(pref_pairs: list[dict]) -> list[str]:
    """Render DPO preference pairs as JSONL lines (``prompt``/``chosen``/``rejected``).

    Args:
        pref_pairs: Rows from `training_db.get_approved_preference_pairs_for_export`.

    Returns:
        One JSON string per pair, ready to be newline-joined.
    """
    lines = []
    for p in pref_pairs:
        obj = {
            "prompt": p["prompt"],
            "chosen": p["chosen_output"],
            "rejected": p["rejected_output"],
        }
        if p.get("domain"):
            obj["domain"] = p["domain"]
        lines.append(json.dumps(obj, ensure_ascii=False))
    return lines


@training_bp.route("/api/workspace/training/export")
def api_export():
    """Build and download a JSONL export of approved training pairs (admin/dev only).

    Query params: ``format`` (one of `EXPORT_FORMATS`, default
    ``"alpaca_jsonl"``), ``domain``, ``min_score``, ``include_auto`` (``"0"``
    to exclude ``auto_approved`` pairs, default include).

    ``dpo_jsonl`` pulls from the DPO preference-pairs table instead of the
    regular pairs table; all other formats pull regular pairs and dispatch to
    the matching ``_build_*`` function.

    On success, logs the export via `training_db.log_export` and records a
    token-usage entry for the requesting user, then streams the JSONL content
    back as a file download.

    Returns:
        A Flask `Response` with ``Content-Disposition: attachment`` and
        ``X-Pair-Count`` header, or a JSON error (400 for unknown format,
        500 on failure).
    """
    user, err = _require_admin_or_dev()
    if err:
        return err

    fmt         = request.args.get("format", "alpaca_jsonl")
    domain      = request.args.get("domain", "")
    min_score   = request.args.get("min_score", 0.0, type=float)
    include_auto= request.args.get("include_auto", "1") != "0"

    if fmt not in EXPORT_FORMATS:
        return jsonify({"error": f"Unknown format. Use one of: {', '.join(EXPORT_FORMATS)}"}), 400

    try:
        if fmt == "dpo_jsonl":
            pref_pairs = training_db.get_approved_preference_pairs_for_export(domain=domain)
            lines = _build_dpo(pref_pairs)
            pair_count = 0
            pref_count = len(pref_pairs)
        else:
            pairs = training_db.get_approved_pairs_for_export(
                domain=domain, min_score=min_score, include_auto=include_auto
            )
            builders = {
                "alpaca_jsonl":    _build_alpaca,
                "chatml_jsonl":    _build_chatml,
                "openai_ft_jsonl": _build_openai_ft,
            }
            lines = builders[fmt](pairs)
            pair_count = len(pairs)
            pref_count = 0

        content = "\n".join(lines) + "\n"
        content_bytes = content.encode("utf-8")
        content_hash  = hashlib.sha256(content_bytes).hexdigest()
        file_name     = f"training_{fmt}_{len(lines)}pairs.jsonl"

        training_db.log_export(
            exported_by=user["id"],
            export_format=fmt,
            pair_count=pair_count,
            preference_count=pref_count,
            filters={"domain": domain, "min_score": min_score, "format": fmt},
            file_name=file_name,
            file_size_bytes=len(content_bytes),
            content_hash=content_hash,
        )

        token_limits.record_tokens(
            user, call_type="workspace_chat", question=f"export:{fmt}:{len(lines)}"
        )

        return Response(
            content_bytes,
            mimetype="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="{file_name}"',
                "Content-Length": str(len(content_bytes)),
                "X-Pair-Count": str(len(lines)),
            },
        )

    except Exception as e:
        logger.error(f"training export failed: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Export history
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/export-history")
def api_export_history():
    """Return the 50 most recent export records (any logged-in user)."""
    user, err = _require_login()
    if err:
        return err
    rows = training_db.get_export_history(50)
    return jsonify({"exports": rows})


# ---------------------------------------------------------------------------
# Preference pairs list
# ---------------------------------------------------------------------------

@training_bp.route("/api/workspace/training/preference-pairs")
def api_preference_pairs():
    """List non-rejected DPO preference pairs, paginated (any logged-in user).

    Query params: ``page``, ``per_page`` (capped at 100).
    """
    user, err = _require_login()
    if err:
        return err
    page     = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 25, type=int), 100)
    rows, total = training_db.get_preference_pairs(page=page, per_page=per_page)
    return jsonify({"pairs": rows, "total": total})
