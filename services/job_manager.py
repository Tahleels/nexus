"""
job_manager.py — Nexus AI · Jobs & Scheduler  (v3 — concurrency-safe)

All CRUD operations now use targeted per-row SQL (UPDATE/DELETE/INSERT WHERE id=?)
instead of the old DELETE-all / INSERT-all pattern, which caused data loss when two
jobs finished or were modified at the same time.
"""

import json
import uuid
from logging_config import get_logger
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from app_db import get_app_db

logger = get_logger(__name__)


def _row_to_job(r) -> Dict:
    """Map an ``app_jobs`` row tuple (column order per `_JOB_SELECT`) to a job dict.

    JSON columns (``tools``, ``schedule``, ``delivery``) are decoded and the
    ``enabled`` bit is coerced to a Python bool.
    """
    return {
        "id":                   r[0],
        "name":                 r[1],
        "description":          r[2] or "",
        "agent_name":           r[3],
        "nlq_prompt":           r[4],
        "tools":                json.loads(r[5]) if r[5] else [],
        "schedule":             json.loads(r[6]) if r[6] else {},
        "delivery":             json.loads(r[7]) if r[7] else {},
        "enabled":              bool(r[8]),
        "created_by":           r[9],
        "created_by_username":  r[10],
        "created_by_role":      r[11],
        "created_at":           str(r[12]) if r[12] else "",
        "updated_at":           str(r[13]) if r[13] else "",
        "last_run":             str(r[14]) if r[14] else None,
        "next_run":             str(r[15]) if r[15] else None,
        "run_count":            r[16],
        "last_status":          r[17],
    }

_JOB_SELECT = """
    SELECT id, name, description, agent_name, nlq_prompt,
           tools, schedule, delivery, enabled,
           created_by, created_by_username, created_by_role,
           created_at, updated_at, last_run, next_run,
           run_count, last_status
    FROM app_jobs
"""

_JOB_INSERT = """
    INSERT INTO app_jobs
        (id, name, description, agent_name, nlq_prompt,
         tools, schedule, delivery, enabled,
         created_by, created_by_username, created_by_role,
         created_at, updated_at, last_run, next_run,
         run_count, last_status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _job_insert_params(j: Dict):
    """Build the positional parameter tuple for `_JOB_INSERT` from a job dict.

    Applies defaults for any missing keys (matching the shape produced by
    `JobManager.create_job`) and JSON-encodes the ``tools``/``schedule``/
    ``delivery`` fields.
    """
    return (
        j.get("id", ""), j.get("name", ""), j.get("description", ""),
        j.get("agent_name", ""), j.get("nlq_prompt", ""),
        json.dumps(j.get("tools", [])),
        json.dumps(j.get("schedule", {})),
        json.dumps(j.get("delivery", {})),
        1 if j.get("enabled", True) else 0,
        j.get("created_by", 0), j.get("created_by_username", "system"),
        j.get("created_by_role", "user"),
        j.get("created_at", datetime.utcnow().isoformat()),
        j.get("updated_at", datetime.utcnow().isoformat()),
        j.get("last_run"), j.get("next_run"),
        j.get("run_count", 0), j.get("last_status"),
    )


class JobManager:
    """CRUD + ownership gateway for scheduled jobs stored in ``app_jobs`` (SQL Server).

    A "job" bundles an agent + NLQ prompt + schedule + delivery config (see
    `_row_to_job` for the full shape). All mutating methods talk directly to
    SQL Server via targeted per-row statements (no in-memory cache, no
    read-all/write-all round trips) so that concurrent job executions or edits
    cannot clobber each other. The module-level singleton `job_manager` is the
    intended entry point; callers should not instantiate `JobManager` directly.
    """

    def __init__(self):
        pass

    # ── bulk read (used by scheduler on startup) ──────────────────────────────

    def load_jobs(self) -> List[Dict]:
        """Return every job in ``app_jobs``, ordered by creation time.

        Returns:
            List of job dicts (see `_row_to_job`), or ``[]`` on error.
        """
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(_JOB_SELECT + " ORDER BY created_at")
                return [_row_to_job(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error loading jobs: {e}")
            return []

    def save_jobs(self, jobs: List[Dict]) -> bool:
        """Full overwrite — only used by one-time JSON migration. Not used in normal operation."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM app_jobs")
                for j in jobs:
                    cursor.execute(_JOB_INSERT, _job_insert_params(j))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving jobs: {e}")
            return False

    # ── CRUD — all per-row, no global load/save ───────────────────────────────

    def create_job(self, data: Dict,
                   created_by: int = 0,
                   created_by_username: str = "system",
                   created_by_role: str = "user") -> Tuple[bool, str, Optional[Dict]]:
        """Create a new job row with a fresh UUID id.

        Args:
            data: Job fields from the request body. Requires ``name``,
                ``agent_name``, ``nlq_prompt``; ``tools``, ``schedule``,
                ``delivery``, ``enabled`` are optional (defaulted).
            created_by: User id of the creator (used for ownership checks).
            created_by_username: Denormalized username, shown in the UI.
            created_by_role: Creator's role at creation time; also used by
                `services.scheduler_service._make_user_context` to decide
                whether scheduled-run token usage is recorded.

        Returns:
            ``(success, message, job_dict)``. On a duplicate job name,
            ``success`` is False and message is "Job name already exists".
        """
        job = {
            "id":                   str(uuid.uuid4()),
            "name":                 data["name"],
            "description":          data.get("description", ""),
            "agent_name":           data["agent_name"],
            "nlq_prompt":           data["nlq_prompt"],
            "tools":                data.get("tools", []),
            "schedule":             data.get("schedule", {}),
            "delivery":             data.get("delivery", {}),
            "enabled":              data.get("enabled", True),
            "created_by":           created_by,
            "created_by_username":  created_by_username,
            "created_by_role":      created_by_role,
            "created_at":           datetime.utcnow().isoformat(),
            "updated_at":           datetime.utcnow().isoformat(),
            "last_run":             None,
            "next_run":             None,
            "run_count":            0,
            "last_status":          None,
        }
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(_JOB_INSERT, _job_insert_params(job))
                conn.commit()
            logger.info(f" Job created: {job['name']} by {created_by_username}")
            return True, "Job created successfully", job
        except Exception as e:
            err = str(e)
            if "uq_app_jobs_name" in err or "UNIQUE" in err.upper():
                return False, "Job name already exists", None
            logger.error(f"Error creating job: {e}")
            return False, "Failed to save job", None

    def get_job(self, job_id: str) -> Optional[Dict]:
        """Fetch a single job by id.

        Args:
            job_id: Job UUID.

        Returns:
            Job dict (see `_row_to_job`), or ``None`` if not found or on error.
        """
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(_JOB_SELECT + " WHERE id=?", job_id)
                row = cursor.fetchone()
                return _row_to_job(row) if row else None
        except Exception as e:
            logger.error(f"Error getting job {job_id}: {e}")
            return None

    def get_all_jobs(self) -> List[Dict]:
        """Alias for `load_jobs` — return every job regardless of owner."""
        return self.load_jobs()

    def get_jobs_for_user(self, user_id: int, role: str) -> List[Dict]:
        """admin → all jobs.  dev / user → only jobs they created."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                if role == "admin":
                    cursor.execute(_JOB_SELECT + " ORDER BY created_at")
                else:
                    cursor.execute(
                        _JOB_SELECT + " WHERE created_by=? ORDER BY created_at",
                        user_id,
                    )
                return [_row_to_job(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting jobs for user {user_id}: {e}")
            return []

    def _check_owner(self, job: Dict, requesting_user_id: int, requesting_role: str) -> bool:
        """Return True if the requester may modify ``job`` (admin, or its creator)."""
        if requesting_role == "admin":
            return True
        return job.get("created_by") == requesting_user_id

    def update_job(self, job_id: str, data: Dict,
                   requesting_user_id: int = 0,
                   requesting_role: str = "admin") -> Tuple[bool, str]:
        """Partially update a job's editable fields, enforcing ownership.

        Only keys present in ``data`` are updated (sparse PATCH semantics);
        ``id``, ``created_by*`` and run-tracking fields are never touched here.

        Args:
            job_id: Job UUID to update.
            data: Subset of ``{name, description, agent_name, nlq_prompt, tools,
                schedule, delivery, enabled}`` to overwrite.
            requesting_user_id: User id making the request.
            requesting_role: Role of the requester; ``"admin"`` bypasses
                ownership checks.

        Returns:
            ``(success, message)``. Fails with "Permission denied..." if the
            requester doesn't own the job and isn't admin, or "Job not found".
        """
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(_JOB_SELECT + " WHERE id=?", job_id)
                row = cursor.fetchone()
                if not row:
                    return False, "Job not found"
                job = _row_to_job(row)
                if not self._check_owner(job, requesting_user_id, requesting_role):
                    return False, "Permission denied: you do not own this job"

                set_parts, values = [], []
                for key in ["name", "description", "agent_name", "nlq_prompt",
                            "tools", "schedule", "delivery", "enabled"]:
                    if key not in data:
                        continue
                    set_parts.append(f"{key} = ?")
                    val = data[key]
                    if key in ("tools", "schedule", "delivery"):
                        values.append(json.dumps(val))
                    elif key == "enabled":
                        values.append(1 if val else 0)
                    else:
                        values.append(val)

                if not set_parts:
                    return True, "Job updated"

                set_parts.append("updated_at = ?")
                values.append(datetime.utcnow().isoformat())
                values.append(job_id)

                cursor.execute(
                    f"UPDATE app_jobs SET {', '.join(set_parts)} WHERE id=?",
                    *values,
                )
                conn.commit()
            return True, "Job updated"
        except Exception as e:
            logger.error(f"Error updating job {job_id}: {e}")
            return False, "Failed to save"

    def delete_job(self, job_id: str,
                   requesting_user_id: int = 0,
                   requesting_role: str = "admin") -> Tuple[bool, str]:
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(_JOB_SELECT + " WHERE id=?", job_id)
                row = cursor.fetchone()
                if not row:
                    return False, "Job not found"
                job = _row_to_job(row)
                if not self._check_owner(job, requesting_user_id, requesting_role):
                    return False, "Permission denied: you do not own this job"
                cursor.execute("DELETE FROM app_jobs WHERE id=?", job_id)
                conn.commit()
            logger.info(f" Job deleted: {job_id}")
            return True, "Job deleted"
        except Exception as e:
            logger.error(f"Error deleting job {job_id}: {e}")
            return False, "Failed to save"

    def toggle_job(self, job_id: str,
                   requesting_user_id: int = 0,
                   requesting_role: str = "admin") -> Tuple[bool, str, Optional[bool]]:
        """Flip a job's ``enabled`` flag, enforcing ownership.

        Args:
            job_id: Job UUID to toggle.
            requesting_user_id: User id making the request.
            requesting_role: Role of the requester; ``"admin"`` bypasses
                ownership checks.

        Returns:
            ``(success, message, new_enabled_state)``. ``new_enabled_state``
            is ``None`` when the toggle failed (not found / permission denied).
        """
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(_JOB_SELECT + " WHERE id=?", job_id)
                row = cursor.fetchone()
                if not row:
                    return False, "Job not found", None
                job = _row_to_job(row)
                if not self._check_owner(job, requesting_user_id, requesting_role):
                    return False, "Permission denied", None
                new_state = not job["enabled"]
                cursor.execute(
                    "UPDATE app_jobs SET enabled=?, updated_at=? WHERE id=?",
                    1 if new_state else 0,
                    datetime.utcnow().isoformat(),
                    job_id,
                )
                conn.commit()
            return True, f"Job {'enabled' if new_state else 'disabled'}", new_state
        except Exception as e:
            logger.error(f"Error toggling job {job_id}: {e}")
            return False, "Failed to save", None

    def update_run_meta(self, job_id: str, status: str,
                        next_run: Optional[str] = None) -> None:
        """Called by scheduler after each execution. Atomically increments run_count."""
        try:
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE app_jobs SET
                        last_run    = ?,
                        last_status = ?,
                        run_count   = run_count + 1,
                        next_run    = COALESCE(?, next_run)
                    WHERE id = ?
                """, datetime.utcnow().isoformat(), status, next_run, job_id)
                conn.commit()
        except Exception as e:
            logger.error(f"Error updating run meta for {job_id}: {e}")


job_manager = JobManager()
