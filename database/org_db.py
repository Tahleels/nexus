"""
org_db.py — Departments, Projects, and Agent Guardrails management.

Tables: departments, projects, user_departments, user_projects, agent_guardrails,
resource_departments, resource_projects, dev_proxy_assignments.
All tables live in the same SQL Server database as auth/app tables, accessed
via app_db.get_app_db() like every other database/*.py module.

This module backs three related but distinct features used across the BI and
Hub Agent systems:
  1. Departments/Projects hierarchy — independent (no parent-child relation)
     organizational groupings that users, agents, jobs, and workflows can be
     assigned to (see get_org_context_for_user(), set_resource_org()).
  2. Agent Guardrails — per-user/department/project data-access rules
     (mandatory SQL filters, table restrictions, custom instructions) that
     app.py injects into the BI chat prompt via get_agent_guardrail() and
     _build_bi_guardrail_prefix().
  3. Dev Proxy Assignments — lets an admin grant a 'dev' role user permission
     to act as ("proxy as") specific other users; see the DEV PROXY
     ASSIGNMENTS section near the bottom of this file. The actual proxy login
     route lives outside this module's scope; this module only stores and
     authorises the assignment.
"""
import json
from logging_config import get_logger
import pyodbc
from app_db import get_app_db

logger = get_logger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _fix(rows: list[dict]) -> list[dict]:
    """Stringify any datetime-like values in each row dict so they are JSON-serialisable."""
    for r in rows:
        for k, v in r.items():
            if hasattr(v, 'isoformat'):
                r[k] = str(v)
    return rows


def _fix_one(row: dict | None) -> dict | None:
    """Same as _fix() but for a single row dict (or None)."""
    return _fix([row])[0] if row else None


def _exec_safe(cur, sql: str, *params):
    """Execute but silently skip if column/table doesn't exist yet."""
    try:
        cur.execute(sql, *params)
    except Exception:
        pass


_TABLE_TO_RTYPE = {
    'app_agents':    'app_agent',
    'app_jobs':      'app_job',
    'hub_agents':    'hub_agent',
    'hub_workflows': 'hub_workflow',
    'hub_jobs':      'hub_job',
}


# ═════════════════════════════════════════════════════════════════════════════
# Departments
# ═════════════════════════════════════════════════════════════════════════════

def get_all_departments() -> list[dict]:
    """Return every department with its member count, ordered by name."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT d.id, d.name, d.description, d.color, d.created_at,
                   COUNT(DISTINCT ud.user_id) AS user_count
            FROM   departments d
            LEFT JOIN user_departments ud ON ud.dept_id = d.id
            GROUP BY d.id, d.name, d.description, d.color, d.created_at
            ORDER BY d.name
        """)
        cols = [c[0] for c in cur.description]
        return _fix([dict(zip(cols, r)) for r in cur.fetchall()])


def get_my_departments(user_id: int) -> list[dict]:
    """Departments the given user belongs to (for non-admin pickers — no full directory)."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT d.id, d.name, d.description, d.color
            FROM   departments d
            JOIN   user_departments ud ON ud.dept_id = d.id
            WHERE  ud.user_id = ?
            ORDER BY d.name
        """, user_id)
        cols = [c[0] for c in cur.description]
        return _fix([dict(zip(cols, r)) for r in cur.fetchall()])


def get_department(dept_id: int) -> dict | None:
    """Return a single department by id, or None if not found."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, description, color, created_at FROM departments WHERE id=?", dept_id)
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return _fix_one(dict(zip(cols, row)))


def create_department(name: str, description: str, color: str, created_by: int) -> tuple[bool, str, int | None]:
    """Create a new department. Returns (success, message, new_dept_id)."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO departments (name, description, color, created_by)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?)
            """, name.strip(), description or '', color or '#6366f1', created_by)
            row = cur.fetchone()
            dept_id = row[0] if row else None
            conn.commit()
        return True, "Department created", dept_id
    except pyodbc.IntegrityError:
        return False, "Department name already exists", None
    except Exception:
        logger.exception("create_department failed")
        return False, "Internal error", None


def update_department(dept_id: int, data: dict) -> tuple[bool, str]:
    """Update a department's name/description/color (only keys present in data are changed)."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            for field in ('name', 'description', 'color'):
                if field in data:
                    cur.execute(f"UPDATE departments SET {field}=? WHERE id=?", data[field], dept_id)
            conn.commit()
        return True, "Department updated"
    except Exception:
        logger.exception("update_department failed")
        return False, "Internal error"


def delete_department(dept_id: int) -> tuple[bool, str]:
    """Delete a department, clearing its references from users, resource junction tables, and legacy single-value columns."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_departments WHERE dept_id=?", dept_id)
            _exec_safe(cur, "DELETE FROM resource_departments WHERE dept_id=?", dept_id)
            _exec_safe(cur, "DELETE FROM agent_guardrails WHERE scope_type='department' AND scope_id=?", dept_id)
            _exec_safe(cur, "UPDATE app_agents SET department_id=NULL WHERE department_id=?", dept_id)
            _exec_safe(cur, "UPDATE app_jobs  SET department_id=NULL WHERE department_id=?", dept_id)
            _exec_safe(cur, "UPDATE hub_agents     SET department_id=NULL WHERE department_id=?", dept_id)
            _exec_safe(cur, "UPDATE hub_workflows  SET department_id=NULL WHERE department_id=?", dept_id)
            _exec_safe(cur, "UPDATE hub_jobs       SET department_id=NULL WHERE department_id=?", dept_id)
            cur.execute("DELETE FROM departments WHERE id=?", dept_id)
            conn.commit()
        return True, "Department deleted"
    except Exception:
        logger.exception("delete_department failed")
        return False, "Internal error"


# ═════════════════════════════════════════════════════════════════════════════
# Projects
# ═════════════════════════════════════════════════════════════════════════════

def get_all_projects() -> list[dict]:
    """Return every project with its member count, ordered by name."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.name, p.description, p.color, p.created_at,
                   COUNT(DISTINCT up.user_id) AS user_count
            FROM   projects p
            LEFT JOIN user_projects up ON up.project_id = p.id
            GROUP BY p.id, p.name, p.description, p.color, p.created_at
            ORDER BY p.name
        """)
        cols = [c[0] for c in cur.description]
        return _fix([dict(zip(cols, r)) for r in cur.fetchall()])


def create_project(name: str, description: str, color: str,
                   created_by: int) -> tuple[bool, str, int | None]:
    """Create a new project. Returns (success, message, new_project_id)."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO projects (name, description, color, created_by)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?)
            """, name.strip(), description or '', color or '#0ea5e9', created_by)
            row = cur.fetchone()
            project_id = row[0] if row else None
            conn.commit()
        return True, "Project created", project_id
    except pyodbc.IntegrityError:
        return False, "Project name already exists", None
    except Exception:
        logger.exception("create_project failed")
        return False, "Internal error", None


def update_project(project_id: int, data: dict) -> tuple[bool, str]:
    """Update a project's name/description/color (only keys present in data are changed)."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            for field in ('name', 'description', 'color'):
                if field in data:
                    cur.execute(f"UPDATE projects SET {field}=? WHERE id=?", data[field], project_id)
            conn.commit()
        return True, "Project updated"
    except Exception:
        logger.exception("update_project failed")
        return False, "Internal error"


def delete_project(project_id: int) -> tuple[bool, str]:
    """Delete a project, clearing its references from users, resource junction tables, and legacy single-value columns."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM user_projects WHERE project_id=?", project_id)
            _exec_safe(cur, "DELETE FROM resource_projects WHERE project_id=?", project_id)
            _exec_safe(cur, "DELETE FROM agent_guardrails WHERE scope_type='project' AND scope_id=?", project_id)
            _exec_safe(cur, "UPDATE app_agents SET project_id=NULL WHERE project_id=?", project_id)
            _exec_safe(cur, "UPDATE app_jobs  SET project_id=NULL WHERE project_id=?", project_id)
            _exec_safe(cur, "UPDATE hub_agents    SET project_id=NULL WHERE project_id=?", project_id)
            _exec_safe(cur, "UPDATE hub_workflows SET project_id=NULL WHERE project_id=?", project_id)
            _exec_safe(cur, "UPDATE hub_jobs      SET project_id=NULL WHERE project_id=?", project_id)
            cur.execute("DELETE FROM projects WHERE id=?", project_id)
            conn.commit()
        return True, "Project deleted"
    except Exception:
        logger.exception("delete_project failed")
        return False, "Internal error"


# ═════════════════════════════════════════════════════════════════════════════
# User ↔ Department / Project assignments
# ═════════════════════════════════════════════════════════════════════════════

def get_user_org_assignments(user_id: int) -> dict:
    """Return a user's department/project ids plus their per-id role, as dict-of-lists/dicts."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT dept_id, role FROM user_departments WHERE user_id=?", user_id)
        dept_rows = cur.fetchall()
        cur.execute("SELECT project_id, role FROM user_projects WHERE user_id=?", user_id)
        proj_rows = cur.fetchall()
    return {
        'dept_ids':      [r[0] for r in dept_rows],
        'dept_roles':    {r[0]: r[1] for r in dept_rows},
        'project_ids':   [r[0] for r in proj_rows],
        'project_roles': {r[0]: r[1] for r in proj_rows},
    }


def get_colleagues(user_id: int) -> list[dict]:
    """
    Return users who share at least one department or project with user_id
    (excluding user_id itself). Used to populate "assign to" pickers for
    regular users without exposing the full company directory.
    """
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT u.id, u.username, r.name AS role
            FROM users u
            JOIN roles r ON r.id = u.role_id
            WHERE u.is_active = 1 AND u.id != ?
              AND (
                u.id IN (
                    SELECT ud2.user_id FROM user_departments ud2
                    WHERE ud2.dept_id IN (SELECT dept_id FROM user_departments WHERE user_id = ?)
                )
                OR u.id IN (
                    SELECT up2.user_id FROM user_projects up2
                    WHERE up2.project_id IN (SELECT project_id FROM user_projects WHERE user_id = ?)
                )
              )
            ORDER BY u.username
        """, user_id, user_id, user_id)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def set_user_departments(user_id: int, dept_assignments: list,
                         assigned_by: int) -> tuple[bool, str]:
    """dept_assignments: list of {id, role} dicts OR plain ints (role defaults to 'member').

    Only touches source='manual' rows — portal-synced rows are never wiped here.
    """
    try:
        with get_app_db() as conn:
            cur = conn.cursor()

            # Resolve and validate dept IDs in one query
            new_items = []
            for item in dept_assignments:
                if isinstance(item, dict):
                    new_items.append((int(item['id']), str(item.get('role', 'member'))))
                else:
                    new_items.append((int(item), 'member'))

            if new_items:
                placeholders = ','.join('?' * len(new_items))
                cur.execute(
                    f"SELECT id FROM departments WHERE id IN ({placeholders})",
                    [i[0] for i in new_items])
                valid_ids = {r[0] for r in cur.fetchall()}
                invalid   = [i[0] for i in new_items if i[0] not in valid_ids]
                if invalid:
                    return False, f"Department ID(s) not found: {invalid}"

            # Only remove manual rows — portal assignments survive untouched
            cur.execute("DELETE FROM user_departments WHERE user_id=? AND source='manual'", user_id)
            for did, role in new_items:
                # Skip if a portal row already exists for this dept (unique constraint on user_id+dept_id)
                cur.execute("""
                    IF NOT EXISTS (SELECT 1 FROM user_departments WHERE user_id=? AND dept_id=?)
                    INSERT INTO user_departments (user_id, dept_id, role, assigned_by, source)
                    VALUES (?,?,?,?,'manual')
                """, user_id, did, user_id, did, role, assigned_by)
            conn.commit()
        return True, "Departments updated"
    except Exception:
        logger.exception("set_user_departments failed")
        return False, "Internal error"


def set_user_projects(user_id: int, project_assignments: list,
                      assigned_by: int) -> tuple[bool, str]:
    """project_assignments: list of {id, role} dicts OR plain ints (role defaults to 'member').

    Only touches source='manual' rows — portal-synced rows are never wiped here.
    """
    try:
        with get_app_db() as conn:
            cur = conn.cursor()

            # Resolve and validate project IDs in one query
            new_items = []
            for item in project_assignments:
                if isinstance(item, dict):
                    new_items.append((int(item['id']), str(item.get('role', 'member'))))
                else:
                    new_items.append((int(item), 'member'))

            if new_items:
                placeholders = ','.join('?' * len(new_items))
                cur.execute(
                    f"SELECT id FROM projects WHERE id IN ({placeholders})",
                    [i[0] for i in new_items])
                valid_ids = {r[0] for r in cur.fetchall()}
                invalid   = [i[0] for i in new_items if i[0] not in valid_ids]
                if invalid:
                    return False, f"Project ID(s) not found: {invalid}"

            # Only remove manual rows — portal assignments survive untouched
            cur.execute("DELETE FROM user_projects WHERE user_id=? AND source='manual'", user_id)
            for pid, role in new_items:
                # Skip if a portal row already exists for this project (unique constraint on user_id+project_id)
                cur.execute("""
                    IF NOT EXISTS (SELECT 1 FROM user_projects WHERE user_id=? AND project_id=?)
                    INSERT INTO user_projects (user_id, project_id, role, assigned_by, source)
                    VALUES (?,?,?,?,'manual')
                """, user_id, pid, user_id, pid, role, assigned_by)
            conn.commit()
        return True, "Projects updated"
    except Exception:
        logger.exception("set_user_projects failed")
        return False, "Internal error"


def get_all_user_org_assignments() -> list[dict]:
    """Return all users with their dept + project assignment counts (for admin table)."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.username, u.email, r.name AS role,
                   COUNT(DISTINCT ud.dept_id)    AS dept_count,
                   COUNT(DISTINCT up.project_id) AS project_count
            FROM   users u
            JOIN   roles r ON r.id = u.role_id
            LEFT JOIN user_departments ud ON ud.user_id = u.id
            LEFT JOIN user_projects    up ON up.user_id = u.id
            GROUP BY u.id, u.username, u.email, r.name
            ORDER BY u.username
        """)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# Resource (agent / job / workflow) dept+project assignment
# ═════════════════════════════════════════════════════════════════════════════

def set_resource_org(table: str, resource_id,
                     dept_ids: list | None,
                     project_ids: list | None) -> tuple[bool, str]:
    """Replace all dept/project assignments for a resource via junction tables.

    Also writes the first value back to the legacy single-value columns so that
    analytics queries (which still use department_id/project_id directly) keep
    working correctly.
    """
    allowed_tables = {'app_agents', 'app_jobs', 'hub_agents', 'hub_workflows', 'hub_jobs'}
    if table not in allowed_tables:
        return False, "Invalid table"
    rtype  = _TABLE_TO_RTYPE[table]
    rid    = str(resource_id)
    dids   = [int(d) for d in (dept_ids or []) if d]
    pids   = [int(p) for p in (project_ids or []) if p]
    id_col = 'name' if table == 'app_agents' else 'id'
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            # Replace junction table rows
            cur.execute(
                "DELETE FROM resource_departments WHERE resource_type=? AND resource_id=?",
                rtype, rid)
            cur.execute(
                "DELETE FROM resource_projects WHERE resource_type=? AND resource_id=?",
                rtype, rid)
            for did in dids:
                cur.execute(
                    "INSERT INTO resource_departments (resource_type, resource_id, dept_id) VALUES (?,?,?)",
                    rtype, rid, did)
            for pid in pids:
                cur.execute(
                    "INSERT INTO resource_projects (resource_type, resource_id, project_id) VALUES (?,?,?)",
                    rtype, rid, pid)
            # Keep legacy columns in sync (first value, or NULL)
            primary_dept = dids[0] if dids else None
            primary_proj = pids[0] if pids else None
            _exec_safe(cur,
                f"UPDATE {table} SET department_id=?, project_id=? WHERE {id_col}=?",
                primary_dept, primary_proj, resource_id)
            conn.commit()
        return True, "Updated"
    except Exception:
        logger.exception("set_resource_org failed")
        return False, "Internal error"


def get_resource_orgs_batch(resource_type: str, resource_ids: list) -> dict:
    """Return {resource_id: {dept_ids: [], project_ids: []}} for a batch of resources."""
    result: dict = {str(rid): {'dept_ids': [], 'project_ids': []} for rid in resource_ids}
    if not resource_ids:
        return result
    try:
        str_ids = [str(r) for r in resource_ids]
        placeholders = ','.join(['?' for _ in str_ids])
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT resource_id, dept_id FROM resource_departments "
                f"WHERE resource_type=? AND resource_id IN ({placeholders})",
                resource_type, *str_ids)
            for row in cur.fetchall():
                key = str(row[0])
                if key in result:
                    result[key]['dept_ids'].append(row[1])
            cur.execute(
                f"SELECT resource_id, project_id FROM resource_projects "
                f"WHERE resource_type=? AND resource_id IN ({placeholders})",
                resource_type, *str_ids)
            for row in cur.fetchall():
                key = str(row[0])
                if key in result:
                    result[key]['project_ids'].append(row[1])
    except Exception:
        logger.exception("get_resource_orgs_batch failed for %s", resource_type)
    return result


def get_resources_org(table: str) -> list[dict]:
    """Return id/name/description + dept_ids/project_ids arrays for all rows in a resource table."""
    allowed_tables = {'app_agents', 'app_jobs', 'hub_agents', 'hub_workflows', 'hub_jobs'}
    if table not in allowed_tables:
        return []
    rtype = _TABLE_TO_RTYPE[table]
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            if table == 'app_agents':
                cur.execute("SELECT name AS id, name, description FROM app_agents ORDER BY name")
            elif table == 'app_jobs':
                cur.execute("SELECT CAST(id AS NVARCHAR(100)) AS id, name, description FROM app_jobs ORDER BY name")
            elif table == 'hub_agents':
                cur.execute("SELECT CAST(id AS NVARCHAR(100)) AS id, name, description FROM hub_agents ORDER BY name")
            elif table == 'hub_workflows':
                cur.execute("SELECT CAST(id AS NVARCHAR(100)) AS id, name, description FROM hub_workflows ORDER BY name")
            else:  # hub_jobs
                cur.execute("SELECT CAST(id AS NVARCHAR(100)) AS id, name, description FROM hub_jobs ORDER BY name")
            cols  = [c[0] for c in cur.description]
            rows  = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not rows:
            return rows

        # Batch-load dept and project assignments from junction tables
        all_ids  = [r['id'] for r in rows]
        org_map  = get_resource_orgs_batch(rtype, all_ids)

        depts    = {d['id']: d for d in get_all_departments()}
        projects = {p['id']: p for p in get_all_projects()}

        for r in rows:
            orgs       = org_map.get(str(r['id']), {})
            dept_ids   = orgs.get('dept_ids', [])
            proj_ids   = orgs.get('project_ids', [])
            r['dept_ids']      = dept_ids
            r['project_ids']   = proj_ids
            r['dept_names']    = [depts[d]['name']  for d in dept_ids if d in depts]
            r['dept_colors']   = [depts[d]['color'] for d in dept_ids if d in depts]
            r['project_names'] = [projects[p]['name'] for p in proj_ids if p in projects]
            # Legacy single-value fields for backward compat
            r['department_id'] = dept_ids[0] if dept_ids else None
            r['project_id']    = proj_ids[0] if proj_ids else None
            r['dept_name']     = r['dept_names'][0]    if r['dept_names']    else None
            r['project_name']  = r['project_names'][0] if r['project_names'] else None
        return rows
    except Exception:
        logger.exception("get_resources_org failed for %s", table)
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Org context — what a user can see (for sidebar selector)
# ═════════════════════════════════════════════════════════════════════════════

def get_org_context_for_user(user_id: int, role: str) -> dict:
    """
    Returns departments + projects visible to the user.
    Admin/dev see all; regular users see only their assigned ones.
    Departments and projects are independent — no parent-child relationship.
    """
    with get_app_db() as conn:
        cur = conn.cursor()
        if role in ('admin', 'dev'):
            cur.execute("SELECT id, name, color FROM departments ORDER BY name")
            depts = [{'id': r[0], 'name': r[1], 'color': r[2], 'role': None} for r in cur.fetchall()]
            cur.execute("SELECT id, name, color FROM projects ORDER BY name")
            projects = [{'id': r[0], 'name': r[1], 'color': r[2], 'role': None} for r in cur.fetchall()]
        else:
            cur.execute("""
                SELECT d.id, d.name, d.color, ud.role
                FROM   departments d
                JOIN   user_departments ud ON ud.dept_id = d.id
                WHERE  ud.user_id = ?
                ORDER BY d.name
            """, user_id)
            depts = [{'id': r[0], 'name': r[1], 'color': r[2], 'role': r[3] or 'member'}
                     for r in cur.fetchall()]

            cur.execute("""
                SELECT p.id, p.name, p.color, up.role
                FROM   projects p
                JOIN   user_projects up ON up.project_id = p.id
                WHERE  up.user_id = ?
                ORDER BY p.name
            """, user_id)
            projects = [{'id': r[0], 'name': r[1], 'color': r[2], 'role': r[3] or 'member'}
                        for r in cur.fetchall()]
    return {'departments': depts, 'projects': projects}


# ═════════════════════════════════════════════════════════════════════════════
# User identity context (for guardrail system prompt injection)
# ═════════════════════════════════════════════════════════════════════════════

def get_user_identity_context(user_id: int) -> dict:
    """Return a user's full org identity: name, email, role, departments, projects."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.username, u.email, r.name AS role
                FROM   users u
                JOIN   roles r ON r.id = u.role_id
                WHERE  u.id = ?
            """, user_id)
            row = cur.fetchone()
            if not row:
                return {}
            username, email, role = row

            cur.execute("""
                SELECT d.name, d.color, ud.role
                FROM   departments d
                JOIN   user_departments ud ON ud.dept_id = d.id
                WHERE  ud.user_id = ?
                ORDER BY d.name
            """, user_id)
            depts = [{'name': r[0], 'color': r[1], 'role': r[2] or 'member'}
                     for r in cur.fetchall()]

            cur.execute("""
                SELECT p.name, p.color, up.role
                FROM   projects p
                JOIN   user_projects up ON up.project_id = p.id
                WHERE  up.user_id = ?
                ORDER BY p.name
            """, user_id)
            projects = [{'name': r[0], 'color': r[1], 'role': r[2] or 'member'}
                        for r in cur.fetchall()]

            return {
                'username':    username,
                'email':       email,
                'role':        role,
                'departments': depts,
                'projects':    projects,
            }
    except Exception:
        logger.exception("get_user_identity_context failed")
        return {}


# ═════════════════════════════════════════════════════════════════════════════
# Agent Guardrails  (per user/department/project, per-agent data access rules)
# ═════════════════════════════════════════════════════════════════════════════

VALID_GUARDRAIL_SCOPES = ('user', 'department', 'project')


def _user_in_scope(user_id: int, scope_type: str, scope_id: int) -> bool:
    """Return True if user is a member of the given department or project.
    Admin/dev users always pass (they have org-wide access).
    Raises on DB error — callers must treat this as a hard failure (fail-closed).
    """
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.name FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ?
        """, user_id)
        row = cur.fetchone()
        if row and row[0] in ('admin', 'dev'):
            return True
        if scope_type == 'department':
            cur.execute("SELECT 1 FROM user_departments WHERE user_id=? AND dept_id=?",
                        user_id, scope_id)
        elif scope_type == 'project':
            cur.execute("SELECT 1 FROM user_projects WHERE user_id=? AND project_id=?",
                        user_id, scope_id)
        else:
            return False
        return cur.fetchone() is not None


def _merge_guardrails(guardrails: list[dict]) -> dict:
    """Combine multiple guardrail configs into one effective guardrail.

    - filter_rules: union of all rules (every rule from every applicable scope
      is mandatory, so they are AND-ed together by the prompt builder).
    - restrict_tables: intersection across scopes that define it (most
      restrictive wins); scopes without a restriction don't narrow it.
    - custom_instruction: all non-empty instructions concatenated.
    """
    filter_rules: list = []
    restrict_tables: set | None = None
    custom_parts: list[str] = []
    for g in guardrails:
        filter_rules.extend(g.get('filter_rules') or [])
        rt = g.get('restrict_tables')
        if rt:
            restrict_tables = set(rt) if restrict_tables is None else (restrict_tables & set(rt))
        ci = (g.get('custom_instruction') or '').strip()
        if ci:
            custom_parts.append(ci)
    return {
        'filter_rules': filter_rules,
        'restrict_tables': sorted(restrict_tables) if restrict_tables is not None else None,
        'custom_instruction': '\n'.join(custom_parts),
    }


def get_scoped_guardrail(scope_type: str, scope_id: int, agent_id: str,
                         agent_type: str = 'hub') -> dict | None:
    """Return the guardrail config for a single (scope, agent) or None if not configured."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT filter_rules, restrict_tables, custom_instruction
                FROM   agent_guardrails
                WHERE  scope_type=? AND scope_id=? AND agent_id=? AND agent_type=?
            """, scope_type, scope_id, agent_id, agent_type)
            row = cur.fetchone()
            if not row:
                return None
            return {
                'filter_rules':      json.loads(row[0]) if row[0] else [],
                'restrict_tables':   json.loads(row[1]) if row[1] else None,
                'custom_instruction': row[2] or '',
            }
    except Exception:
        logger.exception("get_scoped_guardrail failed")
        return None


def set_scoped_guardrail(scope_type: str, scope_id: int, agent_id: str, agent_type: str,
                         filter_rules: list, restrict_tables,
                         custom_instruction: str,
                         created_by: int) -> tuple[bool, str]:
    """Upsert a guardrail config for (scope_type, scope_id, agent)."""
    if scope_type not in VALID_GUARDRAIL_SCOPES:
        return False, f"Invalid scope_type '{scope_type}'"
    try:
        fr_json = json.dumps(filter_rules or [])
        rt_json = json.dumps(restrict_tables) if restrict_tables else None
        user_id_val = scope_id if scope_type == 'user' else None
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                MERGE agent_guardrails AS T
                USING (SELECT ? AS st, ? AS sid, ? AS a, ? AS t) AS S
                ON (T.scope_type=S.st AND T.scope_id=S.sid AND T.agent_id=S.a AND T.agent_type=S.t)
                WHEN MATCHED THEN
                    UPDATE SET filter_rules=?, restrict_tables=?,
                               custom_instruction=?, updated_at=GETUTCDATE()
                WHEN NOT MATCHED THEN
                    INSERT (user_id, scope_type, scope_id, agent_id, agent_type,
                            filter_rules, restrict_tables, custom_instruction, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, scope_type, scope_id, agent_id, agent_type,
                fr_json, rt_json, custom_instruction,
                user_id_val, scope_type, scope_id, agent_id, agent_type,
                fr_json, rt_json, custom_instruction, created_by)
            conn.commit()
        return True, "Guardrail saved"
    except Exception:
        logger.exception("set_scoped_guardrail failed")
        return False, "Internal error"


def delete_scoped_guardrail(scope_type: str, scope_id: int, agent_id: str,
                            agent_type: str = 'hub') -> tuple[bool, str]:
    """Remove the guardrail config for a single (scope_type, scope_id, agent)."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM agent_guardrails
                WHERE  scope_type=? AND scope_id=? AND agent_id=? AND agent_type=?
            """, scope_type, scope_id, agent_id, agent_type)
            conn.commit()
        return True, "Guardrail removed"
    except Exception:
        logger.exception("delete_scoped_guardrail failed")
        return False, "Internal error"


def get_agent_guardrail(user_id: int, agent_id: str,
                        agent_type: str = 'hub',
                        active_scope_type: str | None = None,
                        active_scope_id: int | None = None) -> dict | None:
    """Return the EFFECTIVE guardrail for a user on an agent.

    When active_scope_type + active_scope_id are supplied (e.g. the user has a
    department or project selected in the UI), only that scope's guardrail is
    combined with the user-level guardrail.  This ensures Project X guardrails
    never bleed into Project Y when the same agent is used across both.

    Without an active scope only the user-level guardrail is returned, so
    org-level rules are only enforced when a scope is explicitly active.
    """
    # No try/except — let exceptions propagate so callers can fail-closed.
    # A DB failure here must block the chat, not silently drop guardrails.
    guardrails: list[dict] = []

    user_g = get_scoped_guardrail('user', user_id, agent_id, agent_type)
    if user_g:
        guardrails.append(user_g)

    if active_scope_type and active_scope_id is not None:
        # Verify user belongs to the scope before applying its guardrail.
        # _user_in_scope raises on DB error — exception will propagate up.
        if _user_in_scope(user_id, active_scope_type, active_scope_id):
            scope_g = get_scoped_guardrail(active_scope_type, active_scope_id, agent_id, agent_type)
            if scope_g:
                guardrails.append(scope_g)

    if not guardrails:
        return None
    return _merge_guardrails(guardrails)


def set_agent_guardrail(user_id: int, agent_id: str, agent_type: str,
                        filter_rules: list, restrict_tables,
                        custom_instruction: str,
                        created_by: int) -> tuple[bool, str]:
    """Upsert a per-user guardrail config for (user, agent)."""
    return set_scoped_guardrail('user', user_id, agent_id, agent_type,
                                 filter_rules, restrict_tables, custom_instruction, created_by)


def delete_agent_guardrail(user_id: int, agent_id: str,
                           agent_type: str = 'hub') -> tuple[bool, str]:
    """Remove a per-user guardrail config for (user, agent). Thin wrapper over delete_scoped_guardrail()."""
    return delete_scoped_guardrail('user', user_id, agent_id, agent_type)


def delete_bi_agent_guardrails(agent_name: str) -> None:
    """Remove all guardrail rows (any scope) tied to a BI agent name. Called when a BI agent is deleted."""
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM agent_guardrails WHERE agent_id=? AND agent_type='bi'", agent_name)
        conn.commit()


def get_all_guardrails() -> list[dict]:
    """Return all guardrail rows (any scope) with a resolved display name for admin display."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, scope_type, scope_id, agent_id, agent_type,
                       filter_rules, restrict_tables, custom_instruction,
                       created_at, updated_at
                FROM   agent_guardrails
                ORDER BY scope_type, agent_type, agent_id
            """)
            cols = [c[0] for c in cur.description]
            rows = _fix([dict(zip(cols, r)) for r in cur.fetchall()])

            cur.execute("SELECT id, username FROM users")
            user_names = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT id, name FROM departments")
            dept_names = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT id, name FROM projects")
            project_names = {r[0]: r[1] for r in cur.fetchall()}
            # Resolve hub agent UUIDs → display names for the admin list
            cur.execute("SELECT id, name FROM hub_agents")
            hub_agent_names = {r[0]: r[1] for r in cur.fetchall()}

        name_maps = {'user': user_names, 'department': dept_names, 'project': project_names}
        for r in rows:
            names = name_maps.get(r['scope_type'], {})
            r['scope_name'] = names.get(r['scope_id'], f"{r['scope_type'].title()} #{r['scope_id']}")
            # For hub agents, resolve UUID → name so the admin list shows something readable
            if r.get('agent_type') == 'hub':
                r['agent_display_name'] = hub_agent_names.get(r['agent_id'], r['agent_id'])
            else:
                r['agent_display_name'] = r['agent_id']
            # Back-compat fields expected by older admin UI
            r['user_id'] = r['scope_id'] if r['scope_type'] == 'user' else None
            r['username'] = r['scope_name'] if r['scope_type'] == 'user' else None
        return rows
    except Exception:
        logger.exception("get_all_guardrails failed")
        return []


def get_user_guardrails(user_id: int) -> list[dict]:
    """Return guardrails configured directly on a specific user (not merged with org scopes)."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT agent_id, agent_type, filter_rules, restrict_tables, custom_instruction
                FROM   agent_guardrails
                WHERE  scope_type='user' AND scope_id = ?
                ORDER BY agent_type, agent_id
            """, user_id)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        logger.exception("get_user_guardrails failed")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# DEV PROXY ASSIGNMENTS
# ══════════════════════════════════════════════════════════════════════════════

def get_dev_proxy_users(dev_user_id: int, is_admin: bool = False) -> list[dict]:
    """
    Return the list of users this dev/admin can proxy as.
    Admins can proxy as any user. Devs see only their assigned targets.
    """
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            if is_admin:
                cur.execute("""
                    SELECT id, username, email
                    FROM   users
                    WHERE  id <> ?
                    ORDER BY username
                """, dev_user_id)
            else:
                cur.execute("""
                    SELECT u.id, u.username, u.email
                    FROM   dev_proxy_assignments dpa
                    JOIN   users u ON u.id = dpa.target_user_id
                    WHERE  dpa.dev_user_id = ?
                    ORDER BY u.username
                """, dev_user_id)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        logger.exception("get_dev_proxy_users failed")
        return []


def get_proxy_target_user(target_user_id: int, requesting_user_id: int) -> dict | None:
    """
    Return target user dict if requesting_user_id is authorised to proxy as target.
    Admins are always authorised; devs are checked against dev_proxy_assignments.
    """
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            # Check if requesting user is admin or dev (role via roles JOIN)
            cur.execute("""
                SELECT r.name FROM users u
                JOIN   roles r ON r.id = u.role_id
                WHERE  u.id = ?
            """, requesting_user_id)
            row = cur.fetchone()
            if not row:
                return None
            role = (row[0] or "").lower()

            if role not in ("admin", "dev"):
                return None

            # Check authorisation for devs
            if role == "dev":
                cur.execute("""
                    SELECT 1 FROM dev_proxy_assignments
                    WHERE  dev_user_id = ? AND target_user_id = ?
                """, requesting_user_id, target_user_id)
                if not cur.fetchone():
                    return None

            # Return target user
            cur.execute("""
                SELECT u.id, u.username, u.email, r.name AS role
                FROM   users u
                JOIN   roles r ON r.id = u.role_id
                WHERE  u.id = ?
            """, target_user_id)
            cols = [c[0] for c in cur.description]
            row  = cur.fetchone()
            if row:
                d = dict(zip(cols, row))
                d["_is_proxy"] = True
                return d
    except Exception:
        logger.exception("get_proxy_target_user failed")
    return None


def set_dev_proxy_assignments(dev_user_id: int,
                               target_user_ids: list,
                               assigned_by: int = 0) -> None:
    """Replace the proxy target list for a dev user."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM dev_proxy_assignments WHERE dev_user_id = ?", dev_user_id)
            for tid in target_user_ids:
                cur.execute("""
                    MERGE dev_proxy_assignments AS t
                    USING (SELECT ? AS dev_user_id, ? AS target_user_id) AS s
                          ON t.dev_user_id = s.dev_user_id
                             AND t.target_user_id = s.target_user_id
                    WHEN NOT MATCHED THEN
                        INSERT (dev_user_id, target_user_id, assigned_by)
                        VALUES (?, ?, ?);
                """, int(dev_user_id), int(tid), int(dev_user_id), int(tid), int(assigned_by))
            conn.commit()
    except Exception:
        logger.exception("set_dev_proxy_assignments failed")


def list_all_proxy_assignments() -> list[dict]:
    """Return all proxy assignments with dev + target user names."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT dpa.id, dpa.dev_user_id, du.username AS dev_name,
                       dpa.target_user_id, tu.username AS target_name,
                       dpa.assigned_at
                FROM   dev_proxy_assignments dpa
                JOIN   users du ON du.id = dpa.dev_user_id
                JOIN   users tu ON tu.id = dpa.target_user_id
                ORDER BY du.username, tu.username
            """)
            cols = [c[0] for c in cur.description]
            return _fix([dict(zip(cols, r)) for r in cur.fetchall()])
    except Exception:
        logger.exception("list_all_proxy_assignments failed")
        return []
