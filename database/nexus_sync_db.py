"""
nexus_sync_db.py — external portal integration via cross-database views.

Cross-database views (nexus_view_users, nexus_view_projects,
nexus_view_user_projects) live in this app's database and reference the
configured external portal database (``config.PORTAL_DB_NAME``) directly —
always live, no sync needed.

This module:
  • Queries those views for project/user data
  • Manages the nexus_project_mapping table (admin-configured)
  • Auto-assigns local users to departments/projects on creation
"""
import pyodbc
from logging_config import get_logger
from app_db import get_app_db
import org_db

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# View reads  (always live — data comes straight from the external portal)
# ═════════════════════════════════════════════════════════════════════════════

def get_portal_projects() -> list[dict]:
    """All portal projects via the cross-database view."""
    with get_app_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT nexus_project_id, project_name, is_active
            FROM   nexus_view_projects
            ORDER BY project_name
        """)
        cols = [x[0] for x in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


def _get_portal_user_by_email(email: str) -> dict | None:
    """Look up a single portal user by email via the cross-database view, or None if not found."""
    with get_app_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT DISTINCT nexus_user_id, user_email, first_name, last_name,
                            is_active, employee_id
            FROM   nexus_view_users
            WHERE  user_email = ?
        """, email)
        row = c.fetchone()
        if not row:
            return None
        cols = [x[0] for x in c.description]
        return dict(zip(cols, row))


def _get_portal_assignments_by_email(email: str) -> list[dict]:
    """All project assignments for a user, joined via the combined view."""
    with get_app_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT assignment_id, nexus_user_id, nexus_project_id,
                   manager_id, project_name, project_is_active
            FROM   nexus_view_user_projects
            WHERE  user_email = ?
        """, email)
        cols = [x[0] for x in c.description]
        return [dict(zip(cols, r)) for r in c.fetchall()]


def view_status() -> dict:
    """
    Check whether the cross-database views are queryable right now.
    Returns {ok: bool, users: int, projects: int, error: str|None}.
    """
    try:
        with get_app_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM nexus_view_users")
            u = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM nexus_view_projects")
            p = c.fetchone()[0]
        return {"ok": True, "users": u, "projects": p, "error": None}
    except Exception as exc:
        return {"ok": False, "users": 0, "projects": 0, "error": str(exc)}


# ═════════════════════════════════════════════════════════════════════════════
# Mapping table CRUD  (OUR data — stays in this app's own database)
# ═════════════════════════════════════════════════════════════════════════════

def get_all_mappings() -> list[dict]:
    """Return all mappings enriched with local dept/project names."""
    with get_app_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT m.id, m.nexus_project_id, m.nexus_project_name,
                   m.dept_id,    d.name AS dept_name,
                   m.project_id, p.name AS project_name,
                   m.created_at
            FROM   nexus_project_mapping m
            LEFT JOIN departments d ON d.id = m.dept_id
            LEFT JOIN projects    p ON p.id = m.project_id
            ORDER BY m.nexus_project_name
        """)
        cols = [x[0] for x in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
    for r in rows:
        if hasattr(r.get('created_at'), 'isoformat'):
            r['created_at'] = str(r['created_at'])
    return rows


def upsert_mapping(nexus_project_id: int, nexus_project_name: str,
                   dept_id: int | None, project_id: int | None,
                   created_by: int) -> tuple[bool, str]:
    """Create or update the mapping from a portal project to a local department/project."""
    try:
        with get_app_db() as conn:
            c = conn.cursor()
            c.execute("""
                MERGE nexus_project_mapping AS t
                USING (SELECT ? AS nexus_project_id) AS s
                ON    t.nexus_project_id = s.nexus_project_id
                WHEN MATCHED THEN
                    UPDATE SET nexus_project_name=?, dept_id=?, project_id=?
                WHEN NOT MATCHED THEN
                    INSERT (nexus_project_id, nexus_project_name, dept_id, project_id, created_by)
                    VALUES (?, ?, ?, ?, ?);
            """,
            nexus_project_id,
            nexus_project_name, dept_id, project_id,
            nexus_project_id, nexus_project_name, dept_id, project_id, created_by)
            conn.commit()
        return True, "Mapping saved"
    except Exception as exc:
        logger.exception("upsert_mapping failed")
        return False, str(exc)


def delete_mapping(nexus_project_id: int) -> tuple[bool, str]:
    """Remove a portal project → local dept/project mapping."""
    try:
        with get_app_db() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM nexus_project_mapping WHERE nexus_project_id = ?",
                      nexus_project_id)
            conn.commit()
        return True, "Mapping removed"
    except Exception as exc:
        logger.exception("delete_mapping failed")
        return False, str(exc)


# ═════════════════════════════════════════════════════════════════════════════
# User org auto-assignment
# ═════════════════════════════════════════════════════════════════════════════

def _load_mappings_index() -> dict[int, dict]:
    """Return {nexus_project_id: {dept_id, project_id}} for every configured mapping."""
    with get_app_db() as conn:
        c = conn.cursor()
        c.execute("SELECT nexus_project_id, dept_id, project_id FROM nexus_project_mapping")
        return {r[0]: {"dept_id": r[1], "project_id": r[2]} for r in c.fetchall()}


def sync_user_by_email(local_user_id: int, email: str, assigned_by: int = 0) -> dict:
    """
    Look up a user's portal project assignments (via the live view),
    apply the admin-configured mapping, and assign them to local
    departments / projects.  Writes a record to nexus_user_sync for auditing.
    """
    result = {"synced": False, "assigned_depts": [], "assigned_projects": [], "message": ""}

    try:
        portal_user = _get_portal_user_by_email(email)
    except Exception as exc:
        result["message"] = f"Portal view unavailable: {exc}"
        return result

    if not portal_user:
        result["message"] = f"'{email}' not found in the portal"
        _record_sync(None, local_user_id, email, "no_match")
        return result

    try:
        assignments = _get_portal_assignments_by_email(email)
    except Exception as exc:
        result["message"] = f"Portal view unavailable: {exc}"
        return result

    if not assignments:
        result["synced"] = True
        result["message"] = "User found in the portal but has no project assignments"
        _record_sync(portal_user["nexus_user_id"], local_user_id, email, "synced")
        return result

    mapping_index = _load_mappings_index()
    dept_ids: list[int] = []
    project_ids: list[int] = []

    for a in assignments:
        m = mapping_index.get(a["nexus_project_id"])
        if not m:
            continue
        if m["dept_id"] and m["dept_id"] not in dept_ids:
            dept_ids.append(m["dept_id"])
        if m["project_id"] and m["project_id"] not in project_ids:
            project_ids.append(m["project_id"])

    # Replace ONLY portal-sourced rows — manual assignments are never touched
    _apply_portal_assignments(local_user_id, dept_ids, project_ids, assigned_by)
    result["assigned_depts"]    = dept_ids
    result["assigned_projects"] = project_ids

    _record_sync(portal_user["nexus_user_id"], local_user_id, email, "synced")
    result["synced"] = True
    result["message"] = (
        f"Assigned {len(dept_ids)} dept(s) and {len(project_ids)} project(s) "
        f"from {len(assignments)} portal assignment(s)"
    )
    logger.info("nexus_sync: user %s (%s) → depts=%s projects=%s",
                local_user_id, email, dept_ids, project_ids)
    return result


def _apply_portal_assignments(local_user_id: int, dept_ids: list[int],
                              project_ids: list[int], assigned_by: int) -> None:
    """
    Replace all portal-sourced dept/project rows for this user with the new lists.
    Rows with source='manual' are never touched.
    Passing empty lists clears all portal assignments (mapping was removed).
    """
    with get_app_db() as conn:
        c = conn.cursor()

        # Departments: wipe portal rows, re-insert current set
        c.execute("DELETE FROM user_departments WHERE user_id=? AND source='portal'",
                  local_user_id)
        for did in dept_ids:
            c.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM user_departments
                    WHERE user_id=? AND dept_id=? AND source='manual'
                )
                INSERT INTO user_departments (user_id, dept_id, role, assigned_by, source)
                VALUES (?, ?, 'member', ?, 'portal')
            """, local_user_id, did, local_user_id, did, assigned_by)

        # Projects: wipe portal rows, re-insert current set
        c.execute("DELETE FROM user_projects WHERE user_id=? AND source='portal'",
                  local_user_id)
        for pid in project_ids:
            c.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM user_projects
                    WHERE user_id=? AND project_id=? AND source='manual'
                )
                INSERT INTO user_projects (user_id, project_id, role, assigned_by, source)
                VALUES (?, ?, 'member', ?, 'portal')
            """, local_user_id, pid, local_user_id, pid, assigned_by)

        conn.commit()


def _record_sync(nexus_user_id: int | None, local_user_id: int,
                 email: str, status: str) -> None:
    """Upsert an audit row in nexus_user_sync recording the outcome of a sync attempt for one user."""
    try:
        with get_app_db() as conn:
            c = conn.cursor()
            if nexus_user_id is not None:
                c.execute("""
                    MERGE nexus_user_sync AS t
                    USING (SELECT ? AS nexus_user_id) AS s
                    ON    t.nexus_user_id = s.nexus_user_id
                    WHEN MATCHED THEN
                        UPDATE SET local_user_id=?, user_email=?,
                                   last_synced_at=GETUTCDATE(), sync_status=?
                    WHEN NOT MATCHED THEN
                        INSERT (nexus_user_id, local_user_id, user_email, sync_status)
                        VALUES (?, ?, ?, ?);
                """,
                nexus_user_id,
                local_user_id, email, status,
                nexus_user_id, local_user_id, email, status)
            else:
                c.execute("""
                    IF NOT EXISTS (
                        SELECT 1 FROM nexus_user_sync
                        WHERE  user_email=? AND nexus_user_id IS NULL
                    )
                    INSERT INTO nexus_user_sync
                           (nexus_user_id, local_user_id, user_email, sync_status)
                    VALUES (NULL, ?, ?, ?)
                """, email, local_user_id, email, status)
            conn.commit()
    except Exception as exc:
        logger.warning("_record_sync failed: %s", exc)


def get_portal_user_id(local_user_id: int) -> int | None:
    """Return the portal UserId for a given local app user ID, or None if not synced."""
    try:
        with get_app_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT nexus_user_id FROM nexus_user_sync WHERE local_user_id = ? AND nexus_user_id IS NOT NULL",
                local_user_id,
            )
            row = c.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def get_user_sync_records() -> list[dict]:
    """Return all nexus_user_sync audit rows (newest first) for the admin sync-status view."""
    with get_app_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT s.id, s.nexus_user_id, s.local_user_id, s.user_email,
                   s.last_synced_at, s.sync_status, u.username
            FROM   nexus_user_sync s
            LEFT JOIN users u ON u.id = s.local_user_id
            ORDER BY s.last_synced_at DESC
        """)
        cols = [x[0] for x in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
    for r in rows:
        if hasattr(r.get('last_synced_at'), 'isoformat'):
            r['last_synced_at'] = str(r['last_synced_at'])
    return rows
