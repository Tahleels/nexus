"""
bi_training_db.py — Database layer for BI Tool Training Data.

Captures instruction/output pairs from every BI tool (query, dashboard,
report, infographic, presentation, optimize) so they can be used to
fine-tune Nexus AI's own SLMs — the BI-side counterpart to chat training
data.

Mirrors the pattern of `database/training_db.py`: same connection helper
(`app_db.get_app_db`), same quality-scoring approach (base score +
weighted signals -> 0-100, then bucketed into a review status), and the
same review workflow (pending / auto_approved / approved / needs_review /
rejected) backing ``bi_training_pairs`` instead of ``ws_training_pairs``.
Unlike `training_db.insert_pair`, pairs here are captured live from BI tool
routes via the fire-and-forget `save_async` helper rather than extracted
after the fact from chat history.
"""

import json
from logging_config import get_logger
import threading
import config
from app_db import get_app_db as _get_db  # single source of truth for connections

logger = get_logger(__name__)

_VALID_TOOL_TYPES = {
    "bi_query",
    "bi_dashboard",
    "bi_report",
    "bi_infographic",
    "bi_presentation",
    "bi_presentation_optimize",
}


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def compute_bi_quality_score(
    *,
    tool_type: str = "",
    output_length: int = 0,
    has_sql_query: bool = False,
    feedback_rating: str | None = None,
) -> tuple[float, dict]:
    """
    Returns (score: float 0-100, breakdown: dict).

    Scoring rationale:
      Base                   50
      has_artifact           +5   — BI tools always produce structured output
      long_output (>1000)   +10   — rich JSON response
      has_sql_query          +5   — concrete SQL deliverable (bi_query only)
      complex_tool           +5   — presentation / query are highest-value pairs
      thumbs_up             +15
      thumbs_down           -20
    """
    breakdown = {
        "base":          50,
        "has_artifact":   5,
        "long_output":    0,
        "has_sql_query":  0,
        "complex_tool":   0,
        "thumbs_up":      0,
        "thumbs_down":    0,
    }

    if output_length > 1000:
        breakdown["long_output"] = 10

    if has_sql_query:
        breakdown["has_sql_query"] = 5

    if tool_type in ("bi_presentation", "bi_presentation_optimize", "bi_query"):
        breakdown["complex_tool"] = 5

    if feedback_rating == "thumbs_up":
        breakdown["thumbs_up"] = 15
    elif feedback_rating == "thumbs_down":
        breakdown["thumbs_down"] = -20

    raw = sum(breakdown.values())
    score = max(0.0, min(100.0, float(raw)))
    return score, breakdown


def score_to_status(score: float) -> str:
    """Map a BI quality score to its initial review status.

    Note the BI auto-approve threshold (70) is lower than the chat training
    threshold in `database.training_db.score_to_status` (75), reflecting that
    BI tool outputs are always structured/artifact-producing and thus start
    from a higher quality baseline.

    Args:
        score: Quality score (0-100) from `compute_bi_quality_score`.

    Returns:
        ``"auto_approved"`` (>=70), ``"pending"`` (>=40), or ``"needs_review"``.
    """
    if score >= 70:
        return "auto_approved"
    if score >= 40:
        return "pending"
    return "needs_review"


# ---------------------------------------------------------------------------
# Core insert
# ---------------------------------------------------------------------------

def insert_bi_pair(
    *,
    tool_type: str,
    user_id: int | None = None,
    agent_name: str = "",
    instruction: str,
    context: str = "",
    output: str,
    sql_query: str = "",
    quality_score: float = 60.0,
    quality_breakdown: dict | None = None,
    status: str = "pending",
    model_used: str = "",
    token_count: int | None = None,
    feedback_rating: str | None = None,
    domain: str = "",
    tags: str = "",
) -> int:
    """
    Insert one BI training pair.
    Returns the new row id, or 0 on failure.
    Safe to call from a background thread.
    """
    if tool_type not in _VALID_TOOL_TYPES:
        logger.warning(f"bi_training_db: unknown tool_type '{tool_type}' — skipping")
        return 0
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO bi_training_pairs
                    (tool_type, user_id, agent_name,
                     instruction, context, output, sql_query,
                     quality_score, quality_breakdown, status,
                     model_used, token_count, feedback_rating,
                     domain, tags)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                tool_type,
                user_id,
                (agent_name or "")[:200],
                (instruction or "")[:8000],
                (context or "")[:4000],
                (output or "")[:8000],
                (sql_query or "")[:4000],
                round(quality_score, 2),
                json.dumps(quality_breakdown) if quality_breakdown else None,
                status,
                (model_used or "")[:100],
                token_count,
                feedback_rating,
                (domain or "")[:100],
                (tags or "")[:500],
            )
            row = cursor.fetchone()
            conn.commit()
            pair_id = row[0] if row else 0
            logger.info(
                f"[bi_training] saved {tool_type} pair id={pair_id} "
                f"score={quality_score:.0f} status={status} agent={agent_name!r}"
            )
            return pair_id
    except Exception as e:
        logger.error(f"bi_training_db.insert_bi_pair failed ({tool_type}): {e}")
        return 0


# ---------------------------------------------------------------------------
# Async helper — fire-and-forget
# ---------------------------------------------------------------------------

def save_async(
    *,
    tool_type: str,
    user: dict | None,
    instruction: str,
    context: str = "",
    output: str,
    sql_query: str = "",
    agent_name: str = "",
    model_used: str = "",
    token_count: int | None = None,
    domain: str = "",
    tags: str = "",
) -> None:
    """
    Compute quality score and insert the pair in a daemon thread.
    Call this from route handlers after a successful generation —
    it returns immediately so it never adds latency to the response.
    """
    output_length = len(output or "")
    has_sql       = bool(sql_query and sql_query.strip())
    score, breakdown = compute_bi_quality_score(
        tool_type=tool_type,
        output_length=output_length,
        has_sql_query=has_sql,
    )
    status  = score_to_status(score)
    user_id = (user or {}).get("id")

    def _run():
        insert_bi_pair(
            tool_type       = tool_type,
            user_id         = user_id,
            agent_name      = agent_name,
            instruction     = instruction,
            context         = context,
            output          = output,
            sql_query       = sql_query,
            quality_score   = score,
            quality_breakdown = breakdown,
            status          = status,
            model_used      = model_used,
            token_count     = token_count,
            domain          = domain,
            tags            = tags,
        )

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Read — paginated list
# ---------------------------------------------------------------------------

def get_bi_pairs(
    *,
    tool_type: str = "",
    status: str = "",
    agent_name: str = "",
    min_score: float = 0.0,
    max_score: float = 100.0,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict], int]:
    """List BI training pairs for the review UI, filtered and paginated.

    Results are ordered by quality score (highest first), then recency.

    Args:
        tool_type: Optional exact-match filter (one of `_VALID_TOOL_TYPES`).
        status: Optional exact-match filter on review status.
        agent_name: Optional exact-match filter on the generating agent.
        min_score: Minimum quality_score (inclusive).
        max_score: Maximum quality_score (inclusive).
        page: 1-based page number.
        per_page: Page size.

    Returns:
        ``(rows, total_count)`` — ``rows`` is the current page (with
        ``reviewed_by_name`` joined in and datetimes ISO-formatted),
        ``total_count`` is the full filtered count.
    """
    conditions = ["quality_score >= ?", "quality_score <= ?"]
    params: list = [min_score, max_score]

    if tool_type:
        conditions.append("tool_type = ?")
        params.append(tool_type)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if agent_name:
        conditions.append("agent_name = ?")
        params.append(agent_name)

    where  = "WHERE " + " AND ".join(conditions)
    offset = (page - 1) * per_page

    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM bi_training_pairs {where}", *params)
        total = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT p.id, p.tool_type, p.agent_name,
                   p.instruction, p.output, p.sql_query,
                   p.quality_score, p.status,
                   p.model_used, p.token_count,
                   p.feedback_rating, p.domain, p.tags,
                   p.reviewed_at, p.review_notes, p.created_at,
                   p.quality_breakdown,
                   u.username AS reviewed_by_name
            FROM   bi_training_pairs p
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


def get_bi_pair_by_id(pair_id: int) -> dict | None:
    """Fetch a single BI training pair by id, including the reviewer's username.

    Args:
        pair_id: ``bi_training_pairs.id`` to fetch.

    Returns:
        Full row as a dict (all columns via ``p.*``), or ``None`` if not found.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*, u.username AS reviewed_by_name
            FROM   bi_training_pairs p
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
# Update
# ---------------------------------------------------------------------------

def update_bi_pair_status(pair_id: int, status: str, reviewed_by: int, notes: str = "") -> None:
    """Set a BI pair's review status, recording who reviewed it and when.

    Args:
        pair_id: ``bi_training_pairs.id`` to update.
        status: New status value (e.g. ``"approved"``, ``"rejected"``).
        reviewed_by: User id of the reviewer.
        notes: Optional free-text review notes (truncated to 1000 chars).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE bi_training_pairs
            SET    status       = ?,
                   reviewed_by  = ?,
                   reviewed_at  = GETUTCDATE(),
                   review_notes = ?,
                   updated_at   = GETUTCDATE()
            WHERE  id = ?
        """, status, reviewed_by, (notes or "")[:1000], pair_id)
        conn.commit()


def update_bi_pair_domain_tags(pair_id: int, domain: str, tags: str) -> None:
    """Set/overwrite the domain and tags of a BI training pair (used by the "tag" review action).

    Args:
        pair_id: ``bi_training_pairs.id`` to update.
        domain: New domain value (truncated to 100 chars).
        tags: New comma-separated tags string (truncated to 500 chars).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE bi_training_pairs
            SET    domain = ?, tags = ?, updated_at = GETUTCDATE()
            WHERE  id = ?
        """, (domain or "")[:100], (tags or "")[:500], pair_id)
        conn.commit()


def delete_bi_pair(pair_id: int) -> None:
    """Permanently delete a BI training pair.

    Args:
        pair_id: ``bi_training_pairs.id`` to delete.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bi_training_pairs WHERE id = ?", pair_id)
        conn.commit()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_bi_stats() -> dict:
    """Compute the KPI stats for the BI training review dashboard.

    Returns:
        Dict with ``total_pairs``, per-status counts (``auto_approved``,
        ``approved``, ``pending``, ``needs_review``, ``rejected``),
        ``avg_quality``/``max_quality``/``min_quality`` (rounded to 1 decimal),
        ``export_ready`` (approved + auto_approved count), ``by_tool`` (list
        of ``{tool_type, count}``), and ``by_agent`` (list of
        ``{agent_name, count}``).
    """
    with _get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*)                                               AS total_pairs,
                SUM(CASE WHEN status = 'auto_approved' THEN 1 ELSE 0 END) AS auto_approved,
                SUM(CASE WHEN status = 'approved'      THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status = 'pending'       THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'needs_review'  THEN 1 ELSE 0 END) AS needs_review,
                SUM(CASE WHEN status = 'rejected'      THEN 1 ELSE 0 END) AS rejected,
                AVG(quality_score)                                     AS avg_quality,
                MAX(quality_score)                                     AS max_quality,
                MIN(quality_score)                                     AS min_quality
            FROM bi_training_pairs
        """)
        row  = cursor.fetchone()
        cols = [d[0] for d in cursor.description]
        stats = dict(zip(cols, row))

        stats["export_ready"] = (stats.get("auto_approved") or 0) + (stats.get("approved") or 0)
        for k in ("avg_quality", "max_quality", "min_quality"):
            if stats.get(k) is not None:
                stats[k] = round(float(stats[k]), 1)

        cursor.execute("""
            SELECT tool_type, COUNT(*) AS cnt
            FROM   bi_training_pairs
            GROUP  BY tool_type
            ORDER  BY cnt DESC
        """)
        stats["by_tool"] = [{"tool_type": r[0], "count": r[1]} for r in cursor.fetchall()]

        cursor.execute("""
            SELECT agent_name, COUNT(*) AS cnt
            FROM   bi_training_pairs
            WHERE  agent_name IS NOT NULL AND agent_name != ''
            GROUP  BY agent_name
            ORDER  BY cnt DESC
        """)
        stats["by_agent"] = [{"agent_name": r[0], "count": r[1]} for r in cursor.fetchall()]

    return stats


# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------

def get_approved_bi_pairs_for_export(
    *,
    tool_type: str = "",
    min_score: float = 0.0,
    include_auto: bool = True,
) -> list[dict]:
    """Fetch all export-eligible BI training pairs for JSONL export.

    Mirrors `database.training_db.get_approved_pairs_for_export`, but reads
    from ``bi_training_pairs`` and includes the BI-specific ``sql_query``
    column and ``tool_type`` filter instead of a chat ``workspace_id`` filter.

    Args:
        tool_type: Optional exact-match filter (one of `_VALID_TOOL_TYPES`).
        min_score: Minimum quality_score (inclusive).
        include_auto: When True (default), include ``auto_approved`` pairs in
            addition to manually ``approved`` ones.

    Returns:
        List of dicts with ``id, tool_type, agent_name, instruction, context,
        output, sql_query, domain, tags, model_used, quality_score``, ordered
        by quality_score desc then recency.
    """
    statuses = ["'approved'"]
    if include_auto:
        statuses.append("'auto_approved'")
    status_clause = f"status IN ({', '.join(statuses)})"

    conditions = [status_clause, "quality_score >= ?"]
    params: list = [min_score]
    if tool_type:
        conditions.append("tool_type = ?")
        params.append(tool_type)

    where = "WHERE " + " AND ".join(conditions)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, tool_type, agent_name, instruction, context, output,
                   sql_query, domain, tags, model_used, quality_score
            FROM   bi_training_pairs
            {where}
            ORDER  BY quality_score DESC, created_at DESC
        """, *params)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]
