"""
execution_logger.py — Nexus AI · Jobs & Scheduler  (v3)

Each execution log entry now stores:
  - query_result : { sql_query, columns, data (up to 500 rows), insights, analysis }
  - artifacts    : list of { tool, filename }  (no binary data — kept separate)

The full query_result lets the monitor render the same chat-style output
(table + SQL + insights + AI analysis) that the user sees when chatting.

STORAGE NOTE
  query_result data rows are capped at 500 per execution to keep log files
  manageable.  The row_count field always reflects the true total.
"""

import json
import uuid
from logging_config import get_logger
import math
import decimal
from datetime import date, datetime
from typing   import Dict, List, Optional, Any
from app_db   import get_app_db

logger = get_logger(__name__)

_MAX_STORED_ROWS = 500   # cap rows saved inside the log JSON


def _clean_nan(obj: Any) -> Any:
    """Recursively replace float NaN / datetime objects so json.dumps doesn't choke."""
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj


def _ts() -> str:
    """Return the current UTC time as an ``HH:MM:SS`` string for log_lines entries."""
    return datetime.utcnow().strftime("%H:%M:%S")


# ── helpers ───────────────────────────────────────────────────────────────────

def _row_to_entry(r, include_query_result: bool = True) -> Dict:
    """Map an ``app_job_executions`` row tuple to the execution dict used by the API/UI.

    Args:
        r: A row from one of the SELECTs in this module (column order must match
            ``exec_id, job_id, job_name, triggered_by, status, started_at, finished_at,
            duration_ms, error, tools_completed, row_count, artifacts, query_result,
            log_lines``).
        include_query_result: When False (list views), omit the potentially large
            ``query_result`` payload to keep list responses small.

    Returns:
        Dict with JSON columns already decoded (``tools_completed``, ``artifacts``,
        ``log_lines``, and optionally ``query_result``).
    """
    entry = {
        "exec_id":        r[0],
        "job_id":         r[1],
        "job_name":       r[2],
        "triggered_by":   r[3],
        "status":         r[4],
        "started_at":     str(r[5]) if r[5] else None,
        "finished_at":    str(r[6]) if r[6] else None,
        "duration_ms":    r[7],
        "error":          r[8],
        "tools_completed": json.loads(r[9]) if r[9] else [],
        "row_count":      r[10],
        "artifacts":      json.loads(r[11]) if r[11] else [],
        "log_lines":      json.loads(r[13]) if r[13] else [],
    }
    if include_query_result:
        entry["query_result"] = json.loads(r[12]) if r[12] else None
    return entry


# ── Public API ────────────────────────────────────────────────────────────────

def start_execution(job_id: str, job_name: str,
                    triggered_by: str = "scheduler") -> str:
    """Insert a new 'running' execution row and prune old history for this job.

    Generates a fresh 8-character exec_id, inserts the row with status
    ``running``, and then deletes any executions for ``job_id`` beyond the
    most recent 100 (by ``started_at``) so the log table doesn't grow
    unbounded.

    Args:
        job_id: Owning job's id (``app_jobs.id``).
        job_name: Job display name, denormalized onto the execution row.
        triggered_by: Either ``"scheduler"`` (cron/interval fire) or
            ``"manual"``/similar for a user-initiated "Run now".

    Returns:
        The newly generated exec_id. Returns an exec_id even if the INSERT
        failed (errors are logged, not raised), since callers use it as a
        correlation id for subsequent `append_log`/`finish_execution` calls.
    """
    exec_id = str(uuid.uuid4())[:8]
    log_lines = json.dumps([f"[{_ts()}] Job started (triggered by {triggered_by})"])
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO app_job_executions
                    (exec_id, job_id, job_name, triggered_by, status,
                     started_at, tools_completed, row_count, artifacts, log_lines)
                VALUES (?, ?, ?, ?, 'running', GETUTCDATE(), '[]', 0, '[]', ?)
            """, exec_id, job_id, job_name, triggered_by, log_lines)
            # keep only last 100 executions per job
            cursor.execute("""
                DELETE FROM app_job_executions
                WHERE job_id = ? AND id NOT IN (
                    SELECT TOP 100 id FROM app_job_executions
                    WHERE job_id = ?
                    ORDER BY started_at DESC
                )
            """, job_id, job_id)
            conn.commit()
    except Exception as exc:
        logger.error("start_execution error: %s", exc)
    return exec_id


def append_log(job_id: str, exec_id: str, message: str) -> None:
    """Append a timestamped line to log_lines for a running execution.

    Reads the current ``log_lines`` JSON array, appends ``"[HH:MM:SS] message"``,
    and writes it back. Used throughout `services.scheduler_service._execute_job`
    to build up the step-by-step progress trail shown in the job monitor.

    Args:
        job_id: Owning job's id.
        exec_id: Execution id returned by `start_execution`.
        message: Human-readable progress line (timestamp is prepended automatically).
    """
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT log_lines FROM app_job_executions
                WHERE job_id = ? AND exec_id = ?
            """, job_id, exec_id)
            row = cursor.fetchone()
            if row:
                lines = json.loads(row[0]) if row[0] else []
                lines.append(f"[{_ts()}] {message}")
                cursor.execute("""
                    UPDATE app_job_executions SET log_lines = ?
                    WHERE job_id = ? AND exec_id = ?
                """, json.dumps(lines), job_id, exec_id)
                conn.commit()
    except Exception as exc:
        logger.error("append_log error: %s", exc)


def finish_execution(
    job_id:          str,
    exec_id:         str,
    status:          str,
    error:           Optional[str]        = None,
    tools_completed: Optional[List[str]]  = None,
    row_count:       int                  = 0,
    artifacts:       Optional[List[Dict]] = None,
    sql_query:       Optional[str]        = None,
    columns:         Optional[List[str]]  = None,
    data:            Optional[List[Dict]] = None,
    insights:        Optional[List[Dict]] = None,
) -> None:
    """Finalize a running execution: compute duration, store result, append final log line.

    Builds the ``query_result`` JSON blob (``sql_query``, ``columns``, ``data``
    capped at `_MAX_STORED_ROWS`, ``row_count``, ``insights``, ``truncated``)
    that lets the job monitor render the same chat-style output (table + SQL +
    insights + analysis) the user sees in chat, then writes it together with
    ``status``, ``duration_ms``, ``error``, ``tools_completed`` and ``artifacts``
    to the execution row. Also appends a "Success"/"Failed" line to ``log_lines``.

    Args:
        job_id: Owning job's id.
        exec_id: Execution id returned by `start_execution`.
        status: ``"success"`` or ``"failed"``.
        error: Error message when ``status == "failed"``.
        tools_completed: Names of tools (dashboard/report/infographic/ppt) that
            finished generating.
        row_count: True total row count of the query result (not capped).
        artifacts: List of ``{tool, filename}`` dicts — binary data is kept out
            of this log entry and stored/delivered separately.
        sql_query: The SQL generated/executed for this run, if any.
        columns: Column names of the query result.
        data: Raw result rows; only the first `_MAX_STORED_ROWS` are persisted
            (NaN/datetime/Decimal values are sanitized via `_clean_nan`).
        insights: AI-generated insight objects to store alongside the result.

    Returns:
        None. Errors are caught and logged, never raised, so a logging failure
        cannot crash the job that's already running.
    """
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            # fetch started_at + current log_lines
            cursor.execute("""
                SELECT started_at, log_lines FROM app_job_executions
                WHERE job_id = ? AND exec_id = ?
            """, job_id, exec_id)
            row = cursor.fetchone()
            if not row:
                return

            now = datetime.utcnow()
            try:
                started = datetime.fromisoformat(str(row[0]))
                duration_ms = int((now - started).total_seconds() * 1000)
            except Exception:
                duration_ms = None

            lines = json.loads(row[1]) if row[1] else []
            verb = "✅ Success" if status == "success" else f"❌ Failed: {error}"
            lines.append(f"[{_ts()}] {verb}")

            capped_data = _clean_nan((data or [])[:_MAX_STORED_ROWS])
            query_result = {
                "sql_query": sql_query or "",
                "columns":   columns   or [],
                "data":      capped_data,
                "row_count": row_count,
                "insights":  _clean_nan(insights or []),
                "truncated": len(data or []) > _MAX_STORED_ROWS,
            }

            cursor.execute("""
                UPDATE app_job_executions SET
                    status = ?, finished_at = ?, duration_ms = ?, error = ?,
                    tools_completed = ?, row_count = ?, artifacts = ?,
                    query_result = ?, log_lines = ?
                WHERE job_id = ? AND exec_id = ?
            """,
                status, now.isoformat(), duration_ms, error,
                json.dumps(tools_completed or []),
                row_count,
                json.dumps(artifacts or []),
                json.dumps(query_result),
                json.dumps(lines),
                job_id, exec_id)
            conn.commit()
    except Exception as exc:
        logger.error("finish_execution error: %s", exc)


def get_logs(job_id: str, limit: int = 20) -> List[Dict]:
    """Return most-recent executions first, WITHOUT query_result (for list views).

    Args:
        job_id: Job to fetch execution history for.
        limit: Maximum number of executions to return (most recent first).

    Returns:
        List of execution dicts (see `_row_to_entry`), or ``[]`` on error.
    """
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT TOP (?) exec_id, job_id, job_name, triggered_by, status,
                       started_at, finished_at, duration_ms, error,
                       tools_completed, row_count, artifacts, query_result, log_lines
                FROM app_job_executions
                WHERE job_id = ?
                ORDER BY started_at DESC
            """, limit, job_id)
            return [_row_to_entry(r, include_query_result=False) for r in cursor.fetchall()]
    except Exception as exc:
        logger.error("get_logs error: %s", exc)
        return []


def get_execution(job_id: str, exec_id: str) -> Optional[Dict]:
    """Return a single execution WITH query_result included.

    Backs the ``/api/jobs/logs/<job_id>/<exec_id>/output`` route used to render
    the monitor's chat-style output panel.

    Args:
        job_id: Owning job's id.
        exec_id: Execution id to fetch.

    Returns:
        The execution dict (see `_row_to_entry`), or ``None`` if not found or
        on error.
    """
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT exec_id, job_id, job_name, triggered_by, status,
                       started_at, finished_at, duration_ms, error,
                       tools_completed, row_count, artifacts, query_result, log_lines
                FROM app_job_executions
                WHERE job_id = ? AND exec_id = ?
            """, job_id, exec_id)
            row = cursor.fetchone()
            return _row_to_entry(row, include_query_result=True) if row else None
    except Exception as exc:
        logger.error("get_execution error: %s", exc)
        return None


def get_all_recent_logs(job_ids: List[str], limit: int = 50) -> List[Dict]:
    """Return the most recent executions across multiple jobs, WITHOUT query_result.

    Used by the "all logs" dashboard view (e.g. ``/api/jobs/logs``) to show a
    single combined activity feed for every job a user can see.

    Args:
        job_ids: Job ids to include (typically all jobs visible to the
            requesting user).
        limit: Maximum number of executions to return overall, most recent first.

    Returns:
        List of execution dicts (see `_row_to_entry`), or ``[]`` if ``job_ids``
        is empty or on error.
    """
    if not job_ids:
        return []
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" * len(job_ids))
            cursor.execute(f"""
                SELECT TOP (?) exec_id, job_id, job_name, triggered_by, status,
                       started_at, finished_at, duration_ms, error,
                       tools_completed, row_count, artifacts, query_result, log_lines
                FROM app_job_executions
                WHERE job_id IN ({placeholders})
                ORDER BY started_at DESC
            """, limit, *job_ids)
            return [_row_to_entry(r, include_query_result=False) for r in cursor.fetchall()]
    except Exception as exc:
        logger.error("get_all_recent_logs error: %s", exc)
        return []