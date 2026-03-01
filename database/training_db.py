"""
training_db.py — Database layer for the SLM Training Data module.

Captures instruction/output pairs extracted from Workspace chat conversations
(``ws_messages``) so they can be reviewed, scored, and exported as fine-tuning
data for Nexus AI's own small language models (SLMs). Covers:

  - Quality scoring of a candidate pair (`compute_quality_score`,
    `score_to_status`) from feedback rating, edits, retries, citations, and
    artifact presence.
  - Auto-extraction of user/assistant pairs from a conversation
    (`extract_pairs_from_conversation`, `auto_extract_and_insert`).
  - CRUD over the ``ws_training_pairs`` table (insert/list/update/delete).
  - DPO-style preference pairs (``ws_training_preference_pairs``).
  - Stats and export-history bookkeeping for the review UI
    (`blueprints/training_bp.py`).

All functions use the same pyodbc connection pattern as workspace_db.py /
auth.py, via `app_db.get_app_db`. See `database/bi_training_db.py` for the
parallel module that captures BI tool training pairs instead of chat pairs.
"""

import json
from logging_config import get_logger
import config
from app_db import get_app_db as _get_db  # single source of truth for connections

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def compute_quality_score(
    *,
    feedback_rating: str | None = None,
    is_edited: bool = False,
    retry_count: int = 0,
    response_length: int = 0,
    has_citations: bool = False,
    has_artifact: bool = False,
) -> tuple[float, dict]:
    """
    Returns (score: float 0-100, breakdown: dict).

    Scoring rationale:
      Base              50
      thumbs_up        +25   — explicit human approval
      thumbs_down      -30   — explicit human rejection
      is_edited        -15   — user had to fix the response
      retry            -10   — user tried again (implicit rejection)
      long response    +10   — length > 500 chars signals useful depth
      has citations    +5    — grounded / sourced answer
      has artifact     +5    — response produced actionable output
    """
    breakdown = {
        "base": 50,
        "thumbs_up": 0,
        "thumbs_down": 0,
        "is_edited": 0,
        "retry": 0,
        "long_response": 0,
        "has_citations": 0,
        "has_artifact": 0,
    }

    if feedback_rating == "thumbs_up":
        breakdown["thumbs_up"] = 25
    elif feedback_rating == "thumbs_down":
        breakdown["thumbs_down"] = -30

    if is_edited:
        breakdown["is_edited"] = -15

    if retry_count > 0:
        breakdown["retry"] = -10

    if response_length > 500:
        breakdown["long_response"] = 10

    if has_citations:
        breakdown["has_citations"] = 5

    if has_artifact:
        breakdown["has_artifact"] = 5

    raw = sum(breakdown.values())
    score = max(0.0, min(100.0, float(raw)))
    return score, breakdown


def score_to_status(score: float) -> str:
    """Map a quality score to its initial review status.

    Args:
        score: Quality score (0-100) from `compute_quality_score`.

    Returns:
        ``"auto_approved"`` (>=75), ``"pending"`` (>=40), or ``"needs_review"``.
    """
    if score >= 75:
        return "auto_approved"
    if score >= 40:
        return "pending"
    return "needs_review"


# ---------------------------------------------------------------------------
# Extract training pairs from existing workspace messages
# ---------------------------------------------------------------------------

def extract_pairs_from_conversation(conversation_id: int) -> list[dict]:
    """
    Pull all user→assistant message pairs from a conversation and return
    raw dicts (not yet inserted) so the caller can score + bulk-insert.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, role, content, is_edited, model, total_tokens, created_at
            FROM   ws_messages
            WHERE  conversation_id = ?
              AND  role IN ('user', 'assistant')
              AND  (content IS NOT NULL AND LEN(content) > 10)
            ORDER  BY created_at ASC, id ASC
        """, conversation_id)
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
    msgs = [dict(zip(cols, r)) for r in rows]

    pairs = []
    i = 0
    while i < len(msgs) - 1:
        if msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "assistant":
            pairs.append({"user": msgs[i], "assistant": msgs[i + 1]})
            i += 2
        else:
            i += 1
    return pairs


def _get_feedback_for_message(message_id: int, conn) -> str | None:
    """Return the most recent feedback rating (e.g. ``thumbs_up``/``thumbs_down``) for a message."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TOP 1 rating FROM ws_feedback WHERE message_id = ? ORDER BY created_at DESC",
        message_id,
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _has_citations(message_id: int, conn) -> bool:
    """Return True if the given assistant message has at least one row in ``ws_citations``."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM ws_citations WHERE message_id = ?", message_id
    )
    row = cursor.fetchone()
    return bool(row and row[0] > 0)


def _has_artifact(conversation_id: int, conn) -> bool:
    """Return True if the given conversation produced at least one row in ``ws_artifacts``."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM ws_artifacts WHERE conversation_id = ?", conversation_id
    )
    row = cursor.fetchone()
    return bool(row and row[0] > 0)


def _retry_count(message_id: int, conn) -> int:
    """Return how many times the user retried/regenerated the given assistant message."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM ws_retry_log WHERE message_id = ?", message_id
    )
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def auto_extract_and_insert(conversation_id: int, workspace_id: int | None = None) -> int:
    """
    Extract, score, and insert all eligible pairs for a conversation.
    Skips pairs that already exist (deduped on message_id).
    Returns number of new pairs inserted.
    """
    raw_pairs = extract_pairs_from_conversation(conversation_id)
    if not raw_pairs:
        return 0

    # Fetch already-extracted message IDs
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT message_id FROM ws_training_pairs WHERE conversation_id = ? AND message_id IS NOT NULL",
            conversation_id,
        )
        existing_ids = {row[0] for row in cursor.fetchall()}

    inserted = 0
    for pair in raw_pairs:
        asst = pair["assistant"]
        if asst["id"] in existing_ids:
            continue

        with _get_db() as conn:
            fb      = _get_feedback_for_message(asst["id"], conn)
            cites   = _has_citations(asst["id"], conn)
            artifact = _has_artifact(conversation_id, conn)
            retries  = _retry_count(asst["id"], conn)

        score, breakdown = compute_quality_score(
            feedback_rating=fb,
            is_edited=bool(asst.get("is_edited")),
            retry_count=retries,
            response_length=len(asst.get("content") or ""),
            has_citations=cites,
            has_artifact=artifact,
        )
        status = score_to_status(score)

        insert_pair(
            message_id=asst["id"],
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            instruction=pair["user"]["content"] or "",
            output=asst["content"] or "",
            quality_score=score,
            quality_breakdown=breakdown,
            status=status,
            model_used=asst.get("model"),
            token_count=asst.get("total_tokens"),
            has_citations=cites,
            has_artifact=artifact,
            is_edited=bool(asst.get("is_edited")),
            feedback_rating=fb,
            retry_count=retries,
        )
        inserted += 1

    return inserted


# ---------------------------------------------------------------------------
# CRUD — training pairs
# ---------------------------------------------------------------------------

def insert_pair(
    *,
    message_id=None,
    conversation_id=None,
    workspace_id=None,
    instruction: str,
    context: str = "",
    output: str,
    quality_score: float = 50.0,
    quality_breakdown: dict | None = None,
    status: str = "pending",
    domain: str = "",
    tags: str = "",
    language: str = "en",
    model_used: str = "",
    token_count=None,
    has_citations: bool = False,
    has_artifact: bool = False,
    is_edited: bool = False,
    feedback_rating: str | None = None,
    retry_count: int = 0,
) -> int:
    """Insert one training pair row into ``ws_training_pairs``.

    Text fields are truncated to their column limits (instruction/output to
    8000 chars, context to 4000) before insertion. Called directly by
    `auto_extract_and_insert` for chat-derived pairs, and may also be used to
    insert manually-curated pairs.

    Args:
        message_id: Source ``ws_messages.id`` for the assistant reply, if any.
        conversation_id: Source ``ws_conversations.id``, if any.
        workspace_id: Owning workspace id, if any.
        instruction: The user-side prompt/instruction text.
        context: Optional system/context text (e.g. RAG context block).
        output: The assistant-side response text.
        quality_score: Score from `compute_quality_score` (0-100).
        quality_breakdown: The scoring breakdown dict; stored as JSON.
        status: Review status (``pending`` / ``auto_approved`` / ``approved`` /
            ``needs_review`` / ``rejected``).
        domain: Optional free-text domain/category tag.
        tags: Optional comma-separated tags.
        language: ISO language code, defaults to ``"en"``.
        model_used: Name of the model that generated ``output``.
        token_count: Total tokens for the assistant turn, if known.
        has_citations: Whether the response cited RAG sources.
        has_artifact: Whether the conversation produced an artifact.
        is_edited: Whether the user edited the response.
        feedback_rating: ``thumbs_up``/``thumbs_down``/``None``.
        retry_count: Number of times the user retried this message.

    Returns:
        The new row's id, or 0 if the insert didn't return a row.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_training_pairs
                (message_id, conversation_id, workspace_id, instruction, context, output,
                 quality_score, quality_breakdown, status, domain, tags, language,
                 model_used, token_count, has_citations, has_artifact, is_edited,
                 feedback_rating, retry_count)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            message_id, conversation_id, workspace_id,
            instruction[:8000] if instruction else "",
            context[:4000] if context else "",
            output[:8000] if output else "",
            round(quality_score, 2),
            json.dumps(quality_breakdown) if quality_breakdown else None,
            status,
            (domain or "")[:100],
            (tags or "")[:500],
            language[:10],
            (model_used or "")[:100],
            token_count,
            1 if has_citations else 0,
            1 if has_artifact else 0,
            1 if is_edited else 0,
            feedback_rating,
            retry_count,
        )
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else 0


def get_pairs(
    *,
    status: str = "",
    domain: str = "",
    workspace_id: int | None = None,
    min_score: float = 0.0,
    max_score: float = 100.0,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict], int]:
    """List training pairs for the review UI, filtered and paginated.

    Results are ordered by quality score (highest first), then recency.

    Args:
        status: Optional exact-match filter on review status.
        domain: Optional exact-match filter on domain tag.
        workspace_id: Optional filter to one workspace.
        min_score: Minimum quality_score (inclusive).
        max_score: Maximum quality_score (inclusive).
        page: 1-based page number.
        per_page: Page size.

    Returns:
        ``(rows, total_count)`` — ``rows`` is the current page (with
        ``reviewed_by_name`` joined in and datetimes ISO-formatted),
        ``total_count`` is the full filtered count (for pagination UI).
    """
    conditions = [
        "quality_score >= ?",
        "quality_score <= ?",
    ]
    params: list = [min_score, max_score]

    if status:
        conditions.append("status = ?")
        params.append(status)
    if domain:
        conditions.append("domain = ?")
        params.append(domain)
    if workspace_id:
        conditions.append("workspace_id = ?")
        params.append(workspace_id)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    offset = (page - 1) * per_page

    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM ws_training_pairs {where}", *params)
        total = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT p.id, p.instruction, p.output, p.quality_score, p.status,
                   p.domain, p.tags, p.model_used, p.has_citations, p.has_artifact,
                   p.is_edited, p.feedback_rating, p.retry_count, p.reviewed_at,
                   p.review_notes, p.created_at, p.quality_breakdown,
                   u.username AS reviewed_by_name
            FROM   ws_training_pairs p
            LEFT   JOIN users u ON u.id = p.reviewed_by
            {where}
            ORDER  BY p.quality_score DESC, p.created_at DESC
            OFFSET {offset} ROWS FETCH NEXT {per_page} ROWS ONLY
        """, *params)
        cols = [d[0] for d in cursor.description]
        rows = []
        for r in cursor.fetchall():
            d = dict(zip(cols, r))
            for k in ("created_at", "reviewed_at"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            rows.append(d)

    return rows, total


def update_pair_status(pair_id: int, status: str, reviewed_by: int, notes: str = "") -> None:
    """Set a pair's review status (approve/reject/etc.), recording who reviewed it and when.

    Args:
        pair_id: ``ws_training_pairs.id`` to update.
        status: New status value (e.g. ``"approved"``, ``"rejected"``).
        reviewed_by: User id of the reviewer.
        notes: Optional free-text review notes (truncated to 1000 chars).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_training_pairs
            SET    status      = ?,
                   reviewed_by = ?,
                   reviewed_at = GETUTCDATE(),
                   review_notes = ?,
                   updated_at  = GETUTCDATE()
            WHERE  id = ?
        """, status, reviewed_by, (notes or "")[:1000], pair_id)
        conn.commit()


def update_pair_domain_tags(pair_id: int, domain: str, tags: str) -> None:
    """Set/overwrite the domain and tags of a training pair (used by the "tag" review action).

    Args:
        pair_id: ``ws_training_pairs.id`` to update.
        domain: New domain value (truncated to 100 chars).
        tags: New comma-separated tags string (truncated to 500 chars).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_training_pairs
            SET    domain = ?, tags = ?, updated_at = GETUTCDATE()
            WHERE  id = ?
        """, (domain or "")[:100], (tags or "")[:500], pair_id)
        conn.commit()


def delete_pair(pair_id: int) -> None:
    """Permanently delete a training pair and any annotations attached to it.

    Args:
        pair_id: ``ws_training_pairs.id`` to delete.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ws_training_annotations WHERE pair_id = ?", pair_id)
        cursor.execute("DELETE FROM ws_training_pairs WHERE id = ?", pair_id)
        conn.commit()


def get_pair_by_id(pair_id: int) -> dict | None:
    """Fetch a single training pair by id, including the reviewer's username.

    Args:
        pair_id: ``ws_training_pairs.id`` to fetch.

    Returns:
        Full row as a dict (all columns via ``p.*``), or ``None`` if not found.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*, u.username AS reviewed_by_name
            FROM   ws_training_pairs p
            LEFT   JOIN users u ON u.id = p.reviewed_by
            WHERE  p.id = ?
        """, pair_id)
        cols = [d[0] for d in cursor.description]
        row  = cursor.fetchone()
        if not row:
            return None
        d = dict(zip(cols, row))
        for k in ("created_at", "reviewed_at", "updated_at"):
            if d.get(k) and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        return d


# ---------------------------------------------------------------------------
# Preference pairs
# ---------------------------------------------------------------------------

def insert_preference_pair(
    *,
    base_pair_id: int | None = None,
    conversation_id: int | None = None,
    prompt: str,
    chosen_output: str,
    rejected_output: str,
    chosen_score: float | None = None,
    rejected_score: float | None = None,
    preference_source: str = "human_feedback",
    domain: str = "",
    tags: str = "",
) -> int:
    """Insert a DPO-style preference pair (chosen vs. rejected output for the same prompt).

    Args:
        base_pair_id: Optional ``ws_training_pairs.id`` this preference was derived from.
        conversation_id: Optional source conversation id.
        prompt: The shared prompt/instruction both outputs respond to.
        chosen_output: The preferred response.
        rejected_output: The dispreferred response.
        chosen_score: Optional quality score of the chosen output.
        rejected_score: Optional quality score of the rejected output.
        preference_source: How the preference was determined (default
            ``"human_feedback"``).
        domain: Optional free-text domain tag.
        tags: Optional comma-separated tags.

    Returns:
        The new row's id, or 0 if the insert didn't return a row.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_training_preference_pairs
                (base_pair_id, conversation_id, prompt, chosen_output, rejected_output,
                 chosen_score, rejected_score, preference_source, domain, tags)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            base_pair_id, conversation_id,
            (prompt or "")[:8000],
            (chosen_output or "")[:8000],
            (rejected_output or "")[:8000],
            chosen_score, rejected_score,
            preference_source[:50],
            (domain or "")[:100],
            (tags or "")[:500],
        )
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else 0


def get_preference_pairs(page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
    """List non-rejected DPO preference pairs for the review UI, paginated by recency.

    Args:
        page: 1-based page number.
        per_page: Page size.

    Returns:
        ``(rows, total_count)`` with ``status != 'rejected'`` rows only.
    """
    offset = (page - 1) * per_page
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM ws_training_preference_pairs WHERE status != 'rejected'")
        total = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT pp.id, pp.prompt, pp.chosen_output, pp.rejected_output,
                   pp.chosen_score, pp.rejected_score, pp.preference_source,
                   pp.domain, pp.tags, pp.status, pp.created_at
            FROM   ws_training_preference_pairs pp
            WHERE  pp.status != 'rejected'
            ORDER  BY pp.created_at DESC
            OFFSET {offset} ROWS FETCH NEXT {per_page} ROWS ONLY
        """)
        cols = [d[0] for d in cursor.description]
        rows = []
        for r in cursor.fetchall():
            d = dict(zip(cols, r))
            if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            rows.append(d)
    return rows, total


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_training_stats() -> dict:
    """Compute the KPI stats shown on the training review dashboard.

    Returns:
        Dict with ``total_pairs``, per-status counts (``auto_approved``,
        ``approved``, ``pending``, ``needs_review``, ``rejected``),
        ``avg_quality``/``max_quality``/``min_quality`` (rounded to 1 decimal),
        ``export_ready`` (approved + auto_approved count), ``preference_pairs``
        count, ``total_exports`` count, and ``by_domain`` (list of
        ``{domain, count}`` breakdowns).
    """
    with _get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*)                                         AS total_pairs,
                SUM(CASE WHEN status = 'auto_approved' THEN 1 ELSE 0 END) AS auto_approved,
                SUM(CASE WHEN status = 'approved'      THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status = 'pending'       THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'needs_review'  THEN 1 ELSE 0 END) AS needs_review,
                SUM(CASE WHEN status = 'rejected'      THEN 1 ELSE 0 END) AS rejected,
                AVG(quality_score)                               AS avg_quality,
                MAX(quality_score)                               AS max_quality,
                MIN(quality_score)                               AS min_quality
            FROM ws_training_pairs
        """)
        row = cursor.fetchone()
        cols = [d[0] for d in cursor.description]
        stats = dict(zip(cols, row))

        # approved + auto_approved are export-ready
        stats["export_ready"] = (stats.get("auto_approved") or 0) + (stats.get("approved") or 0)
        for k in ("avg_quality", "max_quality", "min_quality"):
            if stats.get(k) is not None:
                stats[k] = round(float(stats[k]), 1)

        cursor.execute("SELECT COUNT(*) FROM ws_training_preference_pairs WHERE status != 'rejected'")
        stats["preference_pairs"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM ws_training_exports")
        stats["total_exports"] = cursor.fetchone()[0]

        cursor.execute("""
            SELECT domain, COUNT(*) AS cnt
            FROM   ws_training_pairs
            WHERE  domain IS NOT NULL AND domain != ''
            GROUP  BY domain
            ORDER  BY cnt DESC
        """)
        stats["by_domain"] = [{"domain": r[0], "count": r[1]} for r in cursor.fetchall()]

    return stats


def get_export_history(limit: int = 20) -> list[dict]:
    """List past JSONL exports, most recent first, with the exporting user's username joined in.

    Args:
        limit: Maximum number of export records to return.

    Returns:
        List of dicts with ``id, export_format, pair_count, preference_count,
        file_name, file_size_bytes, exported_at, exported_by_name``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT TOP {limit}
                   e.id, e.export_format, e.pair_count, e.preference_count,
                   e.file_name, e.file_size_bytes, e.exported_at,
                   u.username AS exported_by_name
            FROM   ws_training_exports e
            JOIN   users u ON u.id = e.exported_by
            ORDER  BY e.exported_at DESC
        """)
        cols = [d[0] for d in cursor.description]
        rows = []
        for r in cursor.fetchall():
            d = dict(zip(cols, r))
            if d.get("exported_at") and hasattr(d["exported_at"], "isoformat"):
                d["exported_at"] = d["exported_at"].isoformat()
            rows.append(d)
    return rows


def log_export(
    exported_by: int,
    export_format: str,
    pair_count: int,
    preference_count: int,
    filters: dict,
    file_name: str,
    file_size_bytes: int,
    content_hash: str = "",
) -> None:
    """Record a JSONL export event in ``ws_training_exports`` for history/audit purposes.

    Called by `blueprints.training_bp.api_export` after a successful export
    download. Failures are logged, not raised — a logging failure must not
    break the export response already sent to the client.

    Args:
        exported_by: User id who triggered the export.
        export_format: One of `blueprints.training_bp.EXPORT_FORMATS`.
        pair_count: Number of instruction/output pairs included.
        preference_count: Number of DPO preference pairs included.
        filters: The filter dict used to select pairs (domain, min_score, etc.);
            stored as JSON for traceability.
        file_name: Generated download file name.
        file_size_bytes: Size of the exported file in bytes.
        content_hash: SHA-256 hex digest of the exported content, if computed.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_training_exports
                    (exported_by, export_format, pair_count, preference_count,
                     filters, file_name, file_size_bytes, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                exported_by,
                export_format[:30],
                pair_count,
                preference_count,
                json.dumps(filters) if filters else None,
                (file_name or "")[:500],
                file_size_bytes,
                (content_hash or "")[:64],
            )
            conn.commit()
    except Exception as e:
        logger.error(f"training_db.log_export failed: {e}")


# ---------------------------------------------------------------------------
# Export query helpers
# ---------------------------------------------------------------------------

def get_approved_pairs_for_export(
    *,
    domain: str = "",
    min_score: float = 0.0,
    include_auto: bool = True,
) -> list[dict]:
    """Fetch all export-eligible training pairs for JSONL export.

    Args:
        domain: Optional exact-match domain filter.
        min_score: Minimum quality_score (inclusive).
        include_auto: When True (default), include ``auto_approved`` pairs in
            addition to manually ``approved`` ones.

    Returns:
        List of dicts with ``id, instruction, context, output, domain, tags,
        model_used, quality_score, has_citations, has_artifact``, ordered by
        quality_score desc then recency.
    """
    statuses = ["'approved'"]
    if include_auto:
        statuses.append("'auto_approved'")
    status_clause = f"status IN ({', '.join(statuses)})"

    conditions = [status_clause, "quality_score >= ?"]
    params: list = [min_score]
    if domain:
        conditions.append("domain = ?")
        params.append(domain)

    where = "WHERE " + " AND ".join(conditions)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, instruction, context, output, domain, tags, model_used,
                   quality_score, has_citations, has_artifact
            FROM   ws_training_pairs
            {where}
            ORDER  BY quality_score DESC, created_at DESC
        """, *params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


def get_approved_preference_pairs_for_export(domain: str = "") -> list[dict]:
    """Fetch approved DPO preference pairs for export (``dpo_jsonl`` format only).

    Args:
        domain: Optional exact-match domain filter.

    Returns:
        List of dicts with ``id, prompt, chosen_output, rejected_output,
        chosen_score, rejected_score, domain, tags``, ordered by recency.
    """
    where = "WHERE status = 'approved'"
    params: list = []
    if domain:
        where += " AND domain = ?"
        params.append(domain)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, prompt, chosen_output, rejected_output,
                   chosen_score, rejected_score, domain, tags
            FROM   ws_training_preference_pairs
            {where}
            ORDER  BY created_at DESC
        """, *params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]
