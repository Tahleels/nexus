# =============================================================
# workspace_db.py  —  Database layer for Enterprise AI Workspace
# =============================================================
# All CRUD operations for ws_* tables.
# Uses the same pyodbc / config pattern as auth.py.
#
# Sections in this file (in order): project files (RAG uploads), team
# workspaces + membership, projects, conversations, messages, streaming
# chunks (replay), citations, tool calls, artifacts, prompt library,
# enterprise memory, feedback, copy log, retry log, replay events,
# activity log, model usage, analytics, and per-user settings.
#
# Every public function opens its own connection via `_get_db()` (a
# context manager from app_db.get_app_db), runs one or more queries, and
# returns plain `list[dict]` / `dict` / scalar results — datetimes are
# serialised to ISO strings by `_rows_to_dicts`. Functions that only log
# (e.g. log_activity, log_replay_event, save_feedback) swallow exceptions
# and log a warning/error instead of raising, since they are side-channel
# writes that should never break the calling request.
# =============================================================

import json
from logging_config import get_logger

import config
from app_db import get_app_db as _get_db  # single source of truth for connections

logger = get_logger(__name__)


def _rows_to_dicts(cursor) -> list[dict]:
    """Convert all rows of a pyodbc cursor into dicts, ISO-stringifying datetime values.

    Args:
        cursor: An executed pyodbc cursor with a populated ``description``.

    Returns:
        List of dicts (column name -> value), with any value exposing
        ``.isoformat()`` (date/datetime) converted to its ISO string form.
    """
    cols = [d[0] for d in cursor.description]
    rows = []
    for row in cursor.fetchall():
        d = dict(zip(cols, row))
        # Serialise datetime objects
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        rows.append(d)
    return rows


def ensure_schema() -> None:
    """Idempotent migrations for ws_* tables."""
    _migrations = [
        # Project notes — persistent cross-conversation context
        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ws_projects' AND COLUMN_NAME='notes')
           ALTER TABLE ws_projects ADD notes NVARCHAR(MAX) NULL""",
        # Project files — tracks files embedded into LanceDB per project
        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_NAME='ws_project_files')
           CREATE TABLE ws_project_files (
               id          INT IDENTITY(1,1) PRIMARY KEY,
               project_id  INT NOT NULL,
               document_id NVARCHAR(255) NOT NULL,
               filename    NVARCHAR(500) NOT NULL,
               file_type   NVARCHAR(50)  NOT NULL,
               file_size   INT           DEFAULT 0,
               chunk_count INT           DEFAULT 0,
               created_at  DATETIME2     DEFAULT GETUTCDATE()
           )""",
        # Project-level extra tool configuration (admin assigns tools per project)
        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ws_projects' AND COLUMN_NAME='tools_enabled')
           ALTER TABLE ws_projects ADD tools_enabled NVARCHAR(MAX) DEFAULT '[]'""",
        # Memory targeting — admin can assign personal memory to a specific user
        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ws_enterprise_memory' AND COLUMN_NAME='target_user_id')
           ALTER TABLE ws_enterprise_memory ADD target_user_id INT NULL REFERENCES users(id)""",
        # Project visibility — private by default; is_shared=1 makes it visible to all R&D
        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='ws_projects' AND COLUMN_NAME='is_shared')
           ALTER TABLE ws_projects ADD is_shared BIT DEFAULT 0""",
    ]
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            for stmt in _migrations:
                cursor.execute(stmt)
            conn.commit()
    except Exception as exc:
        logger.warning("workspace_db.ensure_schema: %s", exc)


# ─────────────────────────────────────────────────────────────
# PROJECT FILES
# ─────────────────────────────────────────────────────────────

def add_project_file(project_id: int, document_id: str, filename: str,
                     file_type: str, file_size: int, chunk_count: int) -> int:
    """Record a file embedded into LanceDB for a project's RAG context.

    Args:
        project_id: Owning ``ws_projects.id``.
        document_id: LanceDB vector-store document id for this file.
        filename: Original filename.
        file_type: File extension (without leading dot), e.g. "pdf".
        file_size: File size in bytes.
        chunk_count: Number of chunks stored in the vector store for this file.

    Returns:
        New ``ws_project_files.id``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_project_files
                (project_id, document_id, filename, file_type, file_size, chunk_count)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?)
        """, project_id, document_id, filename, file_type, file_size, chunk_count)
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def get_project_files(project_id: int) -> list[dict]:
    """Return all files embedded for a project's RAG context, newest first."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM ws_project_files
            WHERE project_id = ?
            ORDER BY created_at DESC
        """, project_id)
        return _rows_to_dicts(cursor)


def delete_project_file(file_id: int) -> str | None:
    """Delete the DB record and return the document_id so caller can purge LanceDB."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT document_id FROM ws_project_files WHERE id = ?", file_id)
        row = cursor.fetchone()
        if not row:
            return None
        doc_id = row[0]
        cursor.execute("DELETE FROM ws_project_files WHERE id = ?", file_id)
        conn.commit()
        return doc_id


# ─────────────────────────────────────────────────────────────
# WORKSPACES
# ─────────────────────────────────────────────────────────────

def get_workspaces(user_id: int) -> list[dict]:
    """Return non-archived workspaces the user owns or is a member of, newest-updated first."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT w.id, w.owner_id, w.name, w.description, w.color, w.icon,
                   w.is_team, w.is_archived, w.created_at, w.updated_at
            FROM   ws_workspaces w
            WHERE  w.is_archived = 0
              AND  (w.owner_id = ?
                    OR EXISTS (
                        SELECT 1 FROM ws_workspace_members m
                        WHERE m.workspace_id = w.id AND m.user_id = ?
                    ))
            ORDER BY w.updated_at DESC
        """, user_id, user_id)
        return _rows_to_dicts(cursor)


def create_workspace(user_id: int, name: str, description: str = "",
                     color: str = "#6366f1", icon: str = "fa-brain",
                     is_team: bool = False) -> int:
    """Create a new workspace (personal or team) owned by the given user.

    Note: this only creates the ``ws_workspaces`` row — callers (see
    ``workspace_bp.api_create_workspace``) are expected to also add the
    creator as an "owner" member via ``add_workspace_member``.

    Args:
        user_id: Owning user id.
        name: Workspace display name.
        description: Optional description.
        color: Hex color for UI display.
        icon: FontAwesome icon class for UI display.
        is_team: Whether this is a shared team workspace vs. personal.

    Returns:
        New ``ws_workspaces.id``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_workspaces (owner_id, name, description, color, icon, is_team)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?)
        """, user_id, name, description, color, icon, int(is_team))
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def update_workspace(workspace_id: int, data: dict) -> bool:
    """Update allowed fields (name/description/color/icon/is_archived) on a workspace.

    Args:
        workspace_id: ``ws_workspaces.id`` to update.
        data: Dict of fields to set; only keys in the allowed set are applied.

    Returns:
        True if at least one field was updated; False if ``data`` contained
        no recognised fields (no-op, nothing written).
    """
    allowed = {"name", "description", "color", "icon", "is_archived"}
    sets = []
    vals = []
    for k in allowed:
        if k in data:
            sets.append(f"{k} = ?")
            vals.append(data[k])
    if not sets:
        return False
    vals.append(workspace_id)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE ws_workspaces SET {', '.join(sets)}, updated_at = GETUTCDATE() WHERE id = ?",
            *vals
        )
        conn.commit()
    return True


def delete_workspace(workspace_id: int) -> bool:
    """Soft-delete a workspace by setting ``is_archived = 1``. Always returns True."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE ws_workspaces SET is_archived = 1 WHERE id = ?", workspace_id)
        conn.commit()
    return True


# ─────────────────────────────────────────────────────────────
# WORKSPACE MEMBERS
# ─────────────────────────────────────────────────────────────

def get_workspace_members(workspace_id: int) -> list[dict]:
    """Return all members of a workspace (with username/email joined in), oldest-joined first."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.id, m.workspace_id, m.user_id, m.role, m.joined_at,
                   u.username, u.email
            FROM   ws_workspace_members m
            JOIN   users u ON u.id = m.user_id
            WHERE  m.workspace_id = ?
            ORDER BY m.joined_at ASC
        """, workspace_id)
        return _rows_to_dicts(cursor)


def add_workspace_member(workspace_id: int, user_id: int,
                         role: str = "member") -> bool:
    """Add a user to a workspace as a member, if not already present.

    Args:
        workspace_id: ``ws_workspaces.id`` to add the member to.
        user_id: User to add.
        role: One of "owner"/"admin"/"member"/"viewer"; invalid values
            silently fall back to "member".

    Returns:
        True on success (including when the user was already a member, since
        the INSERT is conditional); False if the query raised an exception.
    """
    valid_roles = {"owner", "admin", "member", "viewer"}
    if role not in valid_roles:
        role = "member"
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM ws_workspace_members
                    WHERE workspace_id = ? AND user_id = ?
                )
                INSERT INTO ws_workspace_members (workspace_id, user_id, role)
                VALUES (?, ?, ?)
            """, workspace_id, user_id, workspace_id, user_id, role)
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"add_workspace_member failed: {e}")
        return False


def remove_workspace_member(workspace_id: int, user_id: int) -> bool:
    """Remove a user from a workspace's membership list. Always returns True."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM ws_workspace_members WHERE workspace_id = ? AND user_id = ?",
            workspace_id, user_id
        )
        conn.commit()
    return True


def update_workspace_member_role(workspace_id: int, user_id: int,
                                  role: str) -> bool:
    """Update a member's role within a workspace.

    Args:
        workspace_id: ``ws_workspaces.id``.
        user_id: Member whose role to update.
        role: One of "owner"/"admin"/"member"/"viewer".

    Returns:
        True if the role was valid and the update ran; False if ``role`` is
        not one of the valid roles (no query executed).
    """
    valid_roles = {"owner", "admin", "member", "viewer"}
    if role not in valid_roles:
        return False
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_workspace_members SET role = ?
            WHERE workspace_id = ? AND user_id = ?
        """, role, workspace_id, user_id)
        conn.commit()
    return True


def search_users(query: str, limit: int = 10) -> list[dict]:
    """Return users whose username or email contains query (case-insensitive)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        like = f"%{query}%"
        cursor.execute(f"""
            SELECT TOP ({limit}) u.id, u.username, u.email, r.name AS role
            FROM   users u
            JOIN   roles r ON r.id = u.role_id
            WHERE  u.is_active = 1
              AND  (u.username LIKE ? OR u.email LIKE ?)
            ORDER BY u.username ASC
        """, like, like)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def is_workspace_member(workspace_id: int, user_id: int) -> bool:
    """Return True if the user owns or is a member of the given workspace."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM ws_workspaces WHERE id = ? AND owner_id = ?
            UNION
            SELECT 1 FROM ws_workspace_members WHERE workspace_id = ? AND user_id = ?
        """, workspace_id, user_id, workspace_id, user_id)
        return cursor.fetchone() is not None


# ─────────────────────────────────────────────────────────────
# PROJECTS
# ─────────────────────────────────────────────────────────────

def get_project(project_id: int) -> dict | None:
    """Return a single non-archived project (with owner_username joined in), or None if not found."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.*, u.username AS owner_username
            FROM ws_projects p
            JOIN users u ON u.id = p.owner_id
            WHERE p.id = ? AND p.is_archived = 0
        """, project_id)
        rows = _rows_to_dicts(cursor)
        return rows[0] if rows else None


def get_projects(user_id: int, workspace_id: int = None,
                 is_admin: bool = False) -> list[dict]:
    """Return non-archived projects the user is allowed to see, newest-updated first.

    Visibility rules (when workspace_id is not given):
    - Admin/dev: all projects.
    - Regular user: own projects + projects shared with all R&D (is_shared=1)
      + projects inside any workspace they belong to.
    When workspace_id is given: all projects in that workspace (member-visible).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        if workspace_id:
            cursor.execute("""
                SELECT p.*, u.username AS owner_username
                FROM ws_projects p
                JOIN users u ON u.id = p.owner_id
                WHERE p.workspace_id = ? AND p.is_archived = 0
                ORDER BY p.updated_at DESC
            """, workspace_id)
        elif is_admin:
            cursor.execute("""
                SELECT p.*, u.username AS owner_username
                FROM ws_projects p
                JOIN users u ON u.id = p.owner_id
                WHERE p.is_archived = 0
                ORDER BY p.updated_at DESC
            """)
        else:
            # Own + shared-with-team + in a workspace the user belongs to
            cursor.execute("""
                SELECT p.*, u.username AS owner_username
                FROM ws_projects p
                JOIN users u ON u.id = p.owner_id
                WHERE p.is_archived = 0
                  AND (
                    p.owner_id = ?
                    OR p.is_shared = 1
                    OR p.workspace_id IN (
                        SELECT wm.workspace_id
                        FROM ws_workspace_members wm
                        WHERE wm.user_id = ?
                        UNION
                        SELECT w.id
                        FROM ws_workspaces w
                        WHERE w.owner_id = ?
                    )
                  )
                ORDER BY p.updated_at DESC
            """, user_id, user_id, user_id)
        return _rows_to_dicts(cursor)


def create_project(user_id: int, name: str, description: str = "",
                   system_prompt: str = "", model: str = "gpt-4o",
                   temperature: float = 0.7, workspace_id: int = None,
                   is_shared: bool = False) -> int:
    """Create a new project owned by the user. Private by default (is_shared=False)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_projects
                (workspace_id, owner_id, name, description, system_prompt, model, temperature, is_shared)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, workspace_id, user_id, name, description, system_prompt, model, temperature,
            1 if is_shared else 0)
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def update_project(project_id: int, data: dict) -> bool:
    """Update allowed fields on a project (name/description/system_prompt/notes/model/temperature/is_archived)."""
    allowed = {"name", "description", "system_prompt", "notes", "model", "temperature", "is_archived", "is_shared"}
    sets = []
    vals = []
    for k in allowed:
        if k in data:
            sets.append(f"{k} = ?")
            vals.append(data[k])
    if not sets:
        return False
    vals.append(project_id)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE ws_projects SET {', '.join(sets)}, updated_at = GETUTCDATE() WHERE id = ?",
            *vals
        )
        conn.commit()
    return True


def update_project_tools(project_id: int, tools: list) -> None:
    """Persist the list of extra tool names enabled for a project (admin-only action)."""
    import json as _j
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ws_projects SET tools_enabled = ?, updated_at = GETUTCDATE() WHERE id = ?",
            _j.dumps(tools), project_id
        )
        conn.commit()


def get_all_rd_users() -> list[dict]:
    """Return all users who are in the R&D department (for admin memory targeting)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT u.id, u.username, u.email
            FROM users u
            JOIN user_departments ud ON ud.user_id = u.id
            JOIN departments d ON d.id = ud.dept_id
            WHERE d.name = 'R&D' AND u.is_active = 1
            ORDER BY u.username
        """)
        return _rows_to_dicts(cursor)


def delete_project(project_id: int) -> bool:
    """Soft-delete a project by setting ``is_archived = 1``. Always returns True."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE ws_projects SET is_archived = 1 WHERE id = ?", project_id)
        conn.commit()
    return True


# ─────────────────────────────────────────────────────────────
# CONVERSATIONS
# ─────────────────────────────────────────────────────────────

def get_conversations(user_id: int, project_id: int = None, workspace_id: int = None,
                      include_archived: bool = False, limit: int = 100) -> list[dict]:
    """Return the user's conversations, newest-updated first.

    Args:
        user_id: Owning user id (ignored when ``workspace_id`` is given —
            returns all conversations in that workspace regardless of owner).
        project_id: If given (and no ``workspace_id``), restricts to this
            project's conversations for ``user_id``.
        workspace_id: If given, restricts to this team workspace's
            conversations from any member.
        include_archived: If False (default), excludes archived conversations.
        limit: Max rows to return (``TOP`` clause).

    Returns:
        List of conversation dicts with ``creator_username`` joined in.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        arch_filter = "" if include_archived else "AND c.is_archived = 0"
        if workspace_id:
            # All conversations belonging to this team workspace (any member's)
            cursor.execute(f"""
                SELECT TOP ({limit}) c.*, u.username AS creator_username
                FROM   ws_conversations c
                JOIN   users u ON u.id = c.user_id
                WHERE  c.workspace_id = ? {arch_filter}
                ORDER BY c.updated_at DESC
            """, workspace_id)
        elif project_id:
            cursor.execute(f"""
                SELECT TOP ({limit}) c.*, u.username AS creator_username
                FROM   ws_conversations c
                JOIN   users u ON u.id = c.user_id
                WHERE  c.user_id = ? AND c.project_id = ? {arch_filter}
                ORDER BY c.updated_at DESC
            """, user_id, project_id)
        else:
            cursor.execute(f"""
                SELECT TOP ({limit}) c.*, u.username AS creator_username
                FROM   ws_conversations c
                JOIN   users u ON u.id = c.user_id
                WHERE  c.user_id = ? {arch_filter}
                ORDER BY c.updated_at DESC
            """, user_id)
        return _rows_to_dicts(cursor)


def get_all_starred_conversations(limit: int = 300) -> list[dict]:
    """Return all starred conversations from every team member, newest first."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT TOP ({limit}) c.*, u.username AS creator_username
            FROM   ws_conversations c
            JOIN   users u ON u.id = c.user_id
            WHERE  c.is_starred = 1 AND c.is_archived = 0
            ORDER BY c.updated_at DESC
        """)
        return _rows_to_dicts(cursor)


def get_conversation(conversation_id: int, user_id: int) -> dict | None:
    """Return a conversation if the user is its owner or a member/owner of its workspace.

    Args:
        conversation_id: ``ws_conversations.id`` to fetch.
        user_id: Requesting user, used for the access check.

    Returns:
        Conversation dict (with ``creator_username`` joined in), or None if
        not found or the user lacks access.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        # Allow access if: owner, or member/owner of the workspace it belongs to
        cursor.execute("""
            SELECT c.*, u.username AS creator_username
            FROM   ws_conversations c
            JOIN   users u ON u.id = c.user_id
            WHERE  c.id = ?
              AND (
                c.user_id = ?
                OR EXISTS (
                    SELECT 1 FROM ws_workspace_members m
                    WHERE m.workspace_id = c.workspace_id AND m.user_id = ?
                )
                OR EXISTS (
                    SELECT 1 FROM ws_workspaces w
                    WHERE w.id = c.workspace_id AND w.owner_id = ?
                )
              )
        """, conversation_id, user_id, user_id, user_id)
        rows = _rows_to_dicts(cursor)
        return rows[0] if rows else None


def admin_get_conversation(conversation_id: int) -> dict | None:
    """Return a conversation regardless of ownership (admin/supervisor use only).

    Args:
        conversation_id: ``ws_conversations.id`` to fetch.

    Returns:
        Conversation dict (with ``creator_username`` joined in), or None if
        not found.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.*, u.username AS creator_username, u.email AS creator_email
            FROM   ws_conversations c
            JOIN   users u ON u.id = c.user_id
            WHERE  c.id = ?
        """, conversation_id)
        rows = _rows_to_dicts(cursor)
        return rows[0] if rows else None


def create_conversation(user_id: int, model: str = "gpt-4o",
                        system_prompt: str = "", title: str = None,
                        project_id: int = None, workspace_id: int = None,
                        tools_enabled: list = None) -> int:
    """Create a new conversation thread.

    Args:
        user_id: Creating/owning user id.
        model: Model id to use for this conversation.
        system_prompt: Optional system prompt for this conversation.
        title: Optional initial title (usually None — auto-generated later
            from the first message via ``workspace_openai_service.generate_conversation_title``).
        project_id: Optional owning project.
        workspace_id: Optional owning team workspace.
        tools_enabled: List of enabled tool names (e.g. ``["web_search"]``),
            stored as a JSON string.

    Returns:
        New ``ws_conversations.id``.
    """
    tools_json = json.dumps(tools_enabled or [])
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_conversations
                (user_id, project_id, workspace_id, title, model, system_prompt, tools_enabled)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, user_id, project_id, workspace_id, title, model, system_prompt, tools_json)
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def update_conversation(conversation_id: int, data: dict) -> bool:
    """Update allowed fields on a conversation (title/is_starred/is_archived/system_prompt/model/tools_enabled).

    Args:
        conversation_id: ``ws_conversations.id`` to update.
        data: Dict of fields to set; only keys in the allowed set are
            applied. ``tools_enabled``, if a list, is JSON-serialised.

    Returns:
        True if at least one field was updated; False if ``data`` contained
        no recognised fields.
    """
    allowed = {"title", "is_starred", "is_archived", "system_prompt", "model", "tools_enabled"}
    sets = []
    vals = []
    for k in allowed:
        if k in data:
            sets.append(f"{k} = ?")
            v = data[k]
            if k == "tools_enabled" and isinstance(v, list):
                v = json.dumps(v)
            vals.append(v)
    if not sets:
        return False
    vals.append(conversation_id)
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE ws_conversations SET {', '.join(sets)}, updated_at = GETUTCDATE() WHERE id = ?",
            *vals
        )
        conn.commit()
    return True


def update_conversation_stats(conversation_id: int, tokens_added: int) -> None:
    """Bump a conversation's message_count by 2 (user+assistant turn) and add to its token total.

    Args:
        conversation_id: ``ws_conversations.id`` to update.
        tokens_added: Tokens to add to ``total_tokens`` (typically the
            turn's combined prompt+completion tokens).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_conversations
            SET message_count = message_count + 2,
                total_tokens  = total_tokens  + ?,
                updated_at    = GETUTCDATE()
            WHERE id = ?
        """, tokens_added, conversation_id)
        conn.commit()


def delete_conversation(conversation_id: int) -> bool:
    """Soft-delete a conversation by setting ``is_archived = 1``. Always returns True."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE ws_conversations SET is_archived = 1 WHERE id = ?", conversation_id)
        conn.commit()
    return True


# ─────────────────────────────────────────────────────────────
# MESSAGES
# ─────────────────────────────────────────────────────────────

def get_conversation_messages(conversation_id: int, limit: int = 100) -> list[dict]:
    """Return non-deleted messages for a conversation, in chronological (sequence) order."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT TOP ({limit}) *
            FROM   ws_messages
            WHERE  conversation_id = ? AND is_deleted = 0
            ORDER BY sequence_num ASC, created_at ASC
        """, conversation_id)
        return _rows_to_dicts(cursor)


def save_message(conversation_id: int, user_id: int, role: str,
                 content: str, model: str = None,
                 content_type: str = "text",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 total_tokens: int = 0, latency_ms: int = 0,
                 finish_reason: str = None) -> int:
    """Insert a new message, auto-assigning the next sequence_num for its conversation.

    Args:
        conversation_id: Owning ``ws_conversations.id``.
        user_id: User associated with the message (sender for "user" role;
            the responding user's session for "assistant" role).
        role: "user" or "assistant".
        content: Message text (may be empty — e.g. a streaming placeholder
            later filled in via ``update_message``).
        model: Model id used to produce this message, if applicable.
        content_type: Content type tag, defaults to "text".
        prompt_tokens: Prompt tokens used (0 if not yet known).
        completion_tokens: Completion tokens used (0 if not yet known).
        total_tokens: Total tokens used (0 if not yet known).
        latency_ms: Response latency in milliseconds (0 if not yet known).
        finish_reason: OpenAI finish_reason, if known at insert time.

    Returns:
        New ``ws_messages.id``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        # Get next sequence number
        cursor.execute(
            "SELECT ISNULL(MAX(sequence_num), 0) + 1 FROM ws_messages WHERE conversation_id = ?",
            conversation_id
        )
        seq = cursor.fetchone()[0]

        cursor.execute("""
            INSERT INTO ws_messages
                (conversation_id, user_id, role, content, content_type, model,
                 finish_reason, prompt_tokens, completion_tokens, total_tokens,
                 latency_ms, sequence_num)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, conversation_id, user_id, role, content, content_type, model,
            finish_reason, prompt_tokens, completion_tokens, total_tokens,
            latency_ms, seq)
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def update_message(message_id: int, content: str = None,
                   prompt_tokens: int = 0, completion_tokens: int = 0,
                   total_tokens: int = 0, latency_ms: int = 0,
                   finish_reason: str = None) -> None:
    """Overwrite a message's content and usage/latency stats (unconditional, not a partial patch).

    Used by workspace_bp.py's background DB-flush thread to fill in the
    placeholder assistant message once streaming has completed.

    Args:
        message_id: ``ws_messages.id`` to update.
        content: Final message text (None overwrites with NULL — callers
            should always pass the full text).
        prompt_tokens: Final prompt token count.
        completion_tokens: Final completion token count.
        total_tokens: Final total token count.
        latency_ms: Final response latency in milliseconds.
        finish_reason: OpenAI finish_reason (e.g. "stop").
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_messages
            SET content           = ?,
                prompt_tokens     = ?,
                completion_tokens = ?,
                total_tokens      = ?,
                latency_ms        = ?,
                finish_reason     = ?
            WHERE id = ?
        """, content, prompt_tokens, completion_tokens,
            total_tokens, latency_ms, finish_reason, message_id)
        conn.commit()


def edit_message(message_id: int, user_id: int, new_content: str) -> None:
    """Edit a message's content, archiving the prior content to ws_message_edits.

    Args:
        message_id: ``ws_messages.id`` to edit.
        user_id: User performing the edit (recorded in the edit history row).
        new_content: New message content to store.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        # Archive old content
        cursor.execute("SELECT content FROM ws_messages WHERE id = ?", message_id)
        row = cursor.fetchone()
        old_content = row[0] if row else ""

        cursor.execute("""
            INSERT INTO ws_message_edits (message_id, user_id, old_content, new_content)
            VALUES (?, ?, ?, ?)
        """, message_id, user_id, old_content, new_content)

        cursor.execute("""
            UPDATE ws_messages SET content = ?, is_edited = 1 WHERE id = ?
        """, new_content, message_id)
        conn.commit()


def delete_message(message_id: int) -> None:
    """Soft-delete a message by setting ``is_deleted = 1``."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE ws_messages SET is_deleted = 1 WHERE id = ?", message_id)
        conn.commit()


# ─────────────────────────────────────────────────────────────
# STREAMING CHUNKS
# ─────────────────────────────────────────────────────────────

def save_chunk(message_id: int, chunk_index: int, chunk_text: str,
               chunk_type: str = "text") -> None:
    """Insert a single streaming chunk for replay (one DB write per call — prefer ``batch_save_chunks``).

    Args:
        message_id: Owning ``ws_messages.id``.
        chunk_index: 1-based position of this chunk within the message.
        chunk_text: Chunk text content.
        chunk_type: Chunk type tag, defaults to "text".

    Exceptions are caught and logged rather than raised.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_message_chunks (message_id, chunk_index, chunk_text, chunk_type)
                VALUES (?, ?, ?, ?)
            """, message_id, chunk_index, chunk_text, chunk_type)
            conn.commit()
    except Exception as e:
        logger.error(f"save_chunk failed: {e}")


def batch_save_chunks(message_id: int, chunks: list[tuple]) -> None:
    """Insert all (index, text) chunk tuples in a single DB transaction."""
    if not chunks:
        return
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                "INSERT INTO ws_message_chunks (message_id, chunk_index, chunk_text, chunk_type) VALUES (?, ?, ?, 'text')",
                [(message_id, idx, text) for idx, text in chunks],
            )
            conn.commit()
    except Exception as e:
        logger.error(f"batch_save_chunks failed: {e}")


def get_message_chunks(message_id: int) -> list[dict]:
    """Return a message's streaming chunks in order, for replay."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chunk_index, chunk_text, chunk_type, created_at
            FROM   ws_message_chunks
            WHERE  message_id = ?
            ORDER BY chunk_index ASC
        """, message_id)
        return _rows_to_dicts(cursor)


# ─────────────────────────────────────────────────────────────
# CITATIONS
# ─────────────────────────────────────────────────────────────

def save_citation(message_id: int, citation: dict, index: int = 0) -> None:
    """Insert a web-search citation for a message.

    Args:
        message_id: Owning ``ws_messages.id``.
        citation: Dict with optional ``url``/``title``/``snippet`` keys
            (truncated to 2000/1000/4000 chars respectively).
        index: Citation display order within the message.

    Exceptions are caught and logged rather than raised.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_citations (message_id, url, title, snippet, citation_index)
                VALUES (?, ?, ?, ?, ?)
            """, message_id,
                citation.get("url", "")[:2000],
                (citation.get("title") or "")[:1000],
                (citation.get("snippet") or "")[:4000],
                index)
            conn.commit()
    except Exception as e:
        logger.error(f"save_citation failed: {e}")


def get_message_citations(message_id: int) -> list[dict]:
    """Return a message's web-search citations in display order."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM ws_citations WHERE message_id = ?
            ORDER BY citation_index ASC
        """, message_id)
        return _rows_to_dicts(cursor)


# ─────────────────────────────────────────────────────────────
# TOOL CALLS
# ─────────────────────────────────────────────────────────────

def save_tool_call(message_id: int, conversation_id: int,
                   tool_call_id: str, tool_name: str,
                   tool_input: dict = None) -> int:
    """Insert a tool-call record with status "running" (call ``update_tool_call`` once it finishes).

    Args:
        message_id: Owning ``ws_messages.id``.
        conversation_id: Owning ``ws_conversations.id``.
        tool_call_id: Provider-side tool call identifier.
        tool_name: Name of the tool invoked (e.g. "web_search").
        tool_input: Tool arguments dict, JSON-serialised for storage.

    Returns:
        New ``ws_tool_calls.id``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_tool_calls
                (message_id, conversation_id, tool_call_id, tool_name, tool_input, status)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, 'running')
        """, message_id, conversation_id, tool_call_id, tool_name,
            json.dumps(tool_input or {}))
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def update_tool_call(tool_call_db_id: int, output: str, status: str = "success",
                     latency_ms: int = 0) -> None:
    """Record a tool call's result, status, and latency once it has finished.

    Args:
        tool_call_db_id: ``ws_tool_calls.id`` (the row id returned by
            ``save_tool_call``, not the provider's ``tool_call_id``).
        output: Tool output text (truncated to 4000 chars); falsy values
            are stored as an empty string.
        status: Final status, defaults to "success".
        latency_ms: Tool execution latency in milliseconds.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_tool_calls
            SET tool_output = ?, status = ?, latency_ms = ?
            WHERE id = ?
        """, output[:4000] if output else "", status, latency_ms, tool_call_db_id)
        conn.commit()


# ─────────────────────────────────────────────────────────────
# ARTIFACTS
# ─────────────────────────────────────────────────────────────

def get_artifacts(user_id: int, artifact_type: str = None, workspace_id: int = None,
                  limit: int = 50) -> list[dict]:
    """Return generated artifacts, newest first.

    Args:
        user_id: Currently unused for filtering when ``workspace_id`` is not
            given — all artifacts are shared across the R&D team in that case.
        artifact_type: Optional filter, e.g. "code"/"sql"/"html"/"report".
        workspace_id: If given, restricts to artifacts whose conversation
            belongs to this workspace.
        limit: Max rows to return (``TOP`` clause).

    Returns:
        List of artifact dicts with ``creator_username`` joined in.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        type_cond = "AND a.artifact_type = ?" if artifact_type else ""
        if workspace_id:
            # Artifacts linked to any conversation in this workspace
            params = [workspace_id]
            if artifact_type:
                params.append(artifact_type)
            cursor.execute(f"""
                SELECT TOP ({limit}) a.*, u.username AS creator_username
                FROM ws_artifacts a
                JOIN users u ON u.id = a.user_id
                WHERE a.conversation_id IN (
                    SELECT id FROM ws_conversations
                    WHERE workspace_id = ? AND is_archived = 0
                ) {type_cond}
                ORDER BY a.created_at DESC
            """, *params)
        else:
            # All artifacts are shared across the R&D team
            params = []
            if artifact_type:
                params.append(artifact_type)
            type_where = f"WHERE a.artifact_type = ?" if artifact_type else ""
            cursor.execute(f"""
                SELECT TOP ({limit}) a.*, u.username AS creator_username
                FROM ws_artifacts a
                JOIN users u ON u.id = a.user_id
                {type_where}
                ORDER BY a.created_at DESC
            """, *params)
        return _rows_to_dicts(cursor)


def get_artifact(artifact_id: int, user_id: int) -> dict | None:
    """Return a single artifact if the user owns it or is a member/owner of its conversation's workspace.

    Args:
        artifact_id: ``ws_artifacts.id`` to fetch.
        user_id: Requesting user, used for the access check.

    Returns:
        Artifact dict (with ``creator_username`` joined in), or None if not
        found or the user lacks access.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        # Allow access if owner, or member of the workspace the conversation belongs to
        cursor.execute("""
            SELECT a.*, u.username AS creator_username
            FROM ws_artifacts a
            JOIN users u ON u.id = a.user_id
            WHERE a.id = ?
              AND (
                a.user_id = ?
                OR EXISTS (
                    SELECT 1 FROM ws_conversations c
                    JOIN ws_workspace_members m ON m.workspace_id = c.workspace_id
                    WHERE c.id = a.conversation_id AND m.user_id = ?
                )
                OR EXISTS (
                    SELECT 1 FROM ws_conversations c
                    JOIN ws_workspaces w ON w.id = c.workspace_id
                    WHERE c.id = a.conversation_id AND w.owner_id = ?
                )
              )
        """, artifact_id, user_id, user_id, user_id)
        rows = _rows_to_dicts(cursor)
        return rows[0] if rows else None


def save_artifact(user_id: int, artifact_type: str, title: str,
                  content: str, language: str = None,
                  conversation_id: int = None, message_id: int = None,
                  tags: list = None) -> int:
    """Save a generated artifact (code/SQL/HTML/report, etc.).

    Args:
        user_id: Owning user id.
        artifact_type: Artifact category, e.g. "code"/"sql"/"html"/"report".
        title: Artifact title (truncated to 500 chars; defaults to "Untitled" if falsy).
        content: Artifact body/content.
        language: Optional language/syntax tag (e.g. "python", "sql").
        conversation_id: Optional source conversation.
        message_id: Optional source message.
        tags: Optional list of tag strings, JSON-serialised for storage.

    Returns:
        New ``ws_artifacts.id``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_artifacts
                (conversation_id, message_id, user_id, artifact_type, title, content,
                 language, tags)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, conversation_id, message_id, user_id, artifact_type,
            title[:500] if title else "Untitled",
            content, language,
            json.dumps(tags or []))
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def delete_artifact(artifact_id: int, user_id: int) -> bool:
    """Hard-delete an artifact (only if owned by the given user). Always returns True.

    Note: returns True even if no row matched (e.g. wrong owner) since the
    DELETE statement itself does not raise in that case.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM ws_artifacts WHERE id = ? AND user_id = ?",
            artifact_id, user_id
        )
        conn.commit()
    return True


def update_artifact(artifact_id: int, user_id: int,
                    title: str = None, content: str = None) -> None:
    """Update an artifact's title and/or content (only if owned by the given user).

    Args:
        artifact_id: ``ws_artifacts.id`` to update.
        user_id: Must match the artifact's owner for the update to apply.
        title: New title (truncated to 500 chars), or None to leave unchanged.
        content: New content, or None to leave unchanged.
    """
    parts  = ["updated_at = GETUTCDATE()"]
    params = []
    if title is not None:
        parts.append("title = ?")
        params.append(title[:500])
    if content is not None:
        parts.append("content = ?")
        params.append(content)
    params.extend([artifact_id, user_id])
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE ws_artifacts SET {', '.join(parts)} WHERE id = ? AND user_id = ?",
            *params
        )
        conn.commit()


def toggle_artifact_star(artifact_id: int, user_id: int) -> None:
    """Flip an artifact's ``is_starred`` flag (only if owned by the given user)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_artifacts
            SET is_starred = CASE WHEN is_starred = 1 THEN 0 ELSE 1 END
            WHERE id = ? AND user_id = ?
        """, artifact_id, user_id)
        conn.commit()


# ─────────────────────────────────────────────────────────────
# PROMPT LIBRARY
# ─────────────────────────────────────────────────────────────

def get_prompts(user_id: int, include_shared: bool = True,
                category: str = None, workspace_id: int = None) -> list[dict]:
    """Return prompt-library entries, ordered by use_count then recency.

    Args:
        user_id: Owning user (used to include the user's own prompts).
        include_shared: Currently unused as a separate switch — when
            ``workspace_id`` is given, shared (``is_shared=1``) and own
            prompts are always included; when omitted, all prompts are
            shared across the R&D team (see inline comment in the SQL).
        category: Optional category filter.
        workspace_id: If given, restricts to this workspace's prompts plus
            globally shared and the user's own prompts.

    Returns:
        List of prompt dicts with ``creator_username`` joined in.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cat_filter = "AND p.category = ?" if category else ""
        if workspace_id:
            # Prompts in this workspace + globally shared + user's own
            where = "WHERE (p.workspace_id = ? OR p.is_shared = 1 OR p.user_id = ?)"
            params = [workspace_id, user_id]
        else:
            # All prompts are shared across the R&D team
            where = "WHERE 1=1"
            params = []
        if category:
            params.append(category)
        cursor.execute(f"""
            SELECT p.*, u.username AS creator_username
            FROM ws_prompt_library p
            JOIN users u ON u.id = p.user_id
            {where} {cat_filter}
            ORDER BY p.use_count DESC, p.created_at DESC
        """, *params)
        return _rows_to_dicts(cursor)


def create_prompt(user_id: int, title: str, prompt_text: str,
                  category: str = "general", tags: list = None,
                  is_shared: bool = False,
                  workspace_id: int = None) -> int:
    """Save a new prompt-library entry.

    Args:
        user_id: Owning user id.
        title: Prompt title.
        prompt_text: The reusable prompt text.
        category: Category label, defaults to "general".
        tags: Optional list of tag strings, JSON-serialised for storage.
        is_shared: Whether this prompt is globally shared.
        workspace_id: Optional owning team workspace.

    Returns:
        New ``ws_prompt_library.id``.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_prompt_library
                (user_id, workspace_id, title, prompt_text, category, tags, is_shared)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, user_id, workspace_id, title, prompt_text, category,
            json.dumps(tags or []), int(is_shared))
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def increment_prompt_use(prompt_id: int) -> None:
    """Increment a prompt's use_count by 1. Exceptions are caught and logged rather than raised."""
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE ws_prompt_library SET use_count = use_count + 1 WHERE id = ?",
                prompt_id
            )
            conn.commit()
    except Exception as e:
        logger.error(f"increment_prompt_use failed: {e}")


def delete_prompt(prompt_id: int, user_id: int) -> bool:
    """Hard-delete a prompt-library entry (only if owned by the given user). Always returns True."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM ws_prompt_library WHERE id = ? AND user_id = ?",
            prompt_id, user_id
        )
        conn.commit()
    return True


# ─────────────────────────────────────────────────────────────
# ENTERPRISE MEMORY
# ─────────────────────────────────────────────────────────────

def get_memory(user_id: int, scope: str = None, workspace_id: int = None,
               admin_all: bool = False) -> list[dict]:
    """Return active enterprise-memory entries for a user, ordered by importance then recency.

    Includes:
    - User's own entries (user_id = user_id)
    - Entries where target_user_id = user_id (admin-assigned personal memories)
    - Workspace-scoped entries for the given workspace_id
    - Enterprise-scoped entries (visible to all R&D users)

    Args:
        user_id: Current user's id.
        scope: Optional scope filter (only used for the Memory admin page, not chat injection).
        workspace_id: If given, also fetches workspace-scoped entries for that workspace.
        admin_all: If True, return all entries regardless of user (admin Memory page view).
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        if admin_all:
            cursor.execute("""
                SELECT m.*, u.username AS creator_username,
                       tu.username AS target_username
                FROM ws_enterprise_memory m
                JOIN users u ON u.id = m.user_id
                LEFT JOIN users tu ON tu.id = m.target_user_id
                WHERE m.is_active = 1
                ORDER BY m.importance DESC, m.updated_at DESC
            """)
        elif scope:
            cursor.execute("""
                SELECT m.*, u.username AS creator_username,
                       tu.username AS target_username
                FROM ws_enterprise_memory m
                JOIN users u ON u.id = m.user_id
                LEFT JOIN users tu ON tu.id = m.target_user_id
                WHERE m.is_active = 1 AND m.scope = ?
                  AND (m.user_id = ? OR m.target_user_id = ? OR m.scope = 'enterprise')
                ORDER BY m.importance DESC, m.updated_at DESC
            """, scope, user_id, user_id)
        else:
            # Chat injection: user's own + targeted + workspace + enterprise
            cursor.execute("""
                SELECT m.*, u.username AS creator_username,
                       tu.username AS target_username
                FROM ws_enterprise_memory m
                JOIN users u ON u.id = m.user_id
                LEFT JOIN users tu ON tu.id = m.target_user_id
                WHERE m.is_active = 1
                  AND (
                    m.user_id = ?
                    OR m.target_user_id = ?
                    OR m.scope = 'enterprise'
                    OR (m.scope = 'workspace' AND m.workspace_id = ?)
                  )
                ORDER BY m.importance DESC, m.updated_at DESC
            """, user_id, user_id, workspace_id)
        return _rows_to_dicts(cursor)


def save_memory_entry(user_id: int, memory_key: str, memory_value: str,
                      scope: str = "user", importance: int = 5,
                      workspace_id: int = None,
                      source_conv_id: int = None,
                      target_user_id: int = None) -> int:
    """Save a persistent enterprise-memory fact for the AI to recall later.

    Args:
        user_id: Creator/owner user id.
        memory_key: Short label/key for this fact.
        memory_value: The fact's content/value.
        scope: "user", "workspace", or "enterprise"; defaults to "user".
        importance: 1-10 importance rating used for sort order, defaults to 5.
        workspace_id: Required for scope=="workspace" to scope to a team.
        source_conv_id: Optional conversation this memory was derived from.
        target_user_id: If set (admin only), this personal memory belongs to
            target_user_id instead of the creator — injected into that user's chats.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ws_enterprise_memory
                (user_id, workspace_id, memory_key, memory_value, scope, importance,
                 source_conv_id, target_user_id)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, user_id, workspace_id, memory_key, memory_value,
            scope, importance, source_conv_id, target_user_id)
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def delete_memory_entry(memory_id: int, user_id: int) -> bool:
    """Soft-delete a memory entry (``is_active = 0``), only if owned by the given user. Always returns True."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE ws_enterprise_memory SET is_active = 0
            WHERE id = ? AND user_id = ?
        """, memory_id, user_id)
        conn.commit()
    return True


# ─────────────────────────────────────────────────────────────
# FEEDBACK
# ─────────────────────────────────────────────────────────────

def save_feedback(message_id: int, user_id: int,
                  rating: str, comment: str = "") -> None:
    """Upsert a user's thumbs-up/down feedback on a message (one row per user per message).

    Args:
        message_id: ``ws_messages.id`` being rated.
        user_id: Rating user.
        rating: "thumbs_up" or "thumbs_down".
        comment: Optional free-text comment.

    Exceptions are caught and logged rather than raised.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            # Upsert: one feedback per user per message
            cursor.execute("""
                IF EXISTS (SELECT 1 FROM ws_feedback WHERE message_id = ? AND user_id = ?)
                    UPDATE ws_feedback SET rating = ?, comment = ? WHERE message_id = ? AND user_id = ?
                ELSE
                    INSERT INTO ws_feedback (message_id, user_id, rating, comment)
                    VALUES (?, ?, ?, ?)
            """, message_id, user_id,
                rating, comment, message_id, user_id,
                message_id, user_id, rating, comment)
            conn.commit()
    except Exception as e:
        logger.error(f"save_feedback failed: {e}")


# ─────────────────────────────────────────────────────────────
# COPY LOG
# ─────────────────────────────────────────────────────────────

def log_copy(message_id: int, user_id: int, copy_type: str = "text") -> None:
    """Log that a user copied a message's content. Exceptions are caught and logged rather than raised."""
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_copy_log (message_id, user_id, copy_type)
                VALUES (?, ?, ?)
            """, message_id, user_id, copy_type)
            conn.commit()
    except Exception as e:
        logger.error(f"log_copy failed: {e}")


# ─────────────────────────────────────────────────────────────
# RETRY LOG
# ─────────────────────────────────────────────────────────────

def log_retry(conversation_id: int, message_id: int, user_id: int,
              attempt_num: int, error_msg: str = "") -> None:
    """Log a retried request (e.g. after a streaming/tool error).

    Args:
        conversation_id: Owning ``ws_conversations.id``.
        message_id: The message being retried.
        user_id: User who triggered the retry.
        attempt_num: 1-based attempt counter.
        error_msg: Optional error message from the failed attempt.

    Exceptions are caught and logged rather than raised.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_retry_log
                    (conversation_id, message_id, user_id, attempt_num, error_msg)
                VALUES (?, ?, ?, ?, ?)
            """, conversation_id, message_id, user_id, attempt_num, error_msg)
            conn.commit()
    except Exception as e:
        logger.error(f"log_retry failed: {e}")


# ─────────────────────────────────────────────────────────────
# ACTIVITY LOG
# ─────────────────────────────────────────────────────────────

def log_activity(user_id: int, action: str, entity_type: str = None,
                 entity_id: int = None, details: dict = None,
                 ip_address: str = None) -> None:
    """Append a row to the workspace activity audit log.

    Args:
        user_id: Acting user.
        action: Action label (e.g. "create_conversation", "delete_artifact").
        entity_type: Optional entity type the action applies to (e.g. "conversation").
        entity_id: Optional id of the affected entity.
        details: Optional dict of extra context, JSON-serialised for storage.
        ip_address: Optional requester IP (truncated to 45 chars).

    Exceptions are caught and logged rather than raised.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_activity_log
                    (user_id, action, entity_type, entity_id, details, ip_address)
                VALUES (?, ?, ?, ?, ?, ?)
            """, user_id, action, entity_type, entity_id,
                json.dumps(details or {}, default=str),
                (ip_address or "")[:45])
            conn.commit()
    except Exception as e:
        logger.error(f"log_activity failed ({action}): {e}")


def get_activity(user_id: int, limit: int = 100) -> list[dict]:
    """Return a single user's recent activity log entries, newest first."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT TOP ({limit}) a.*, u.username
            FROM   ws_activity_log a
            JOIN   users u ON u.id = a.user_id
            WHERE  a.user_id = ?
            ORDER BY a.created_at DESC
        """, user_id)
        return _rows_to_dicts(cursor)


def get_all_activity(limit: int = 200) -> list[dict]:
    """Return recent activity log entries across all users, newest first (admin view)."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT TOP ({limit}) a.*, u.username
            FROM   ws_activity_log a
            JOIN   users u ON u.id = a.user_id
            ORDER BY a.created_at DESC
        """)
        return _rows_to_dicts(cursor)


# ─────────────────────────────────────────────────────────────
# MODEL USAGE
# ─────────────────────────────────────────────────────────────

def record_model_usage(user_id: int, model: str,
                       conversation_id: int,
                       prompt_tokens: int, completion_tokens: int,
                       cost: float = 0.0,
                       call_type: str = "chat") -> None:
    """Record one model call's token usage and estimated cost for analytics.

    Args:
        user_id: Calling user.
        model: Model id used.
        conversation_id: Owning conversation.
        prompt_tokens: Prompt/input tokens consumed.
        completion_tokens: Completion/output tokens generated.
        cost: Estimated USD cost (e.g. from
            ``workspace_openai_service.estimate_cost``).
        call_type: Call type tag, defaults to "chat".

    Exceptions are caught and logged rather than raised.
    """
    try:
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ws_model_usage
                    (user_id, model, conversation_id, prompt_tokens,
                     completion_tokens, total_tokens, estimated_cost_usd, call_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, user_id, model, conversation_id,
                prompt_tokens, completion_tokens,
                prompt_tokens + completion_tokens, cost, call_type)
            conn.commit()
    except Exception as e:
        logger.error(f"record_model_usage failed: {e}")


def get_model_usage_summary(user_id: int, days: int = 30) -> list[dict]:
    """Return per-model token/cost/call-count totals for a user over the trailing N days.

    Args:
        user_id: User to summarise.
        days: Lookback window in days.

    Returns:
        List of dicts (one per model) with total_tokens, prompt_tokens,
        completion_tokens, estimated_cost, call_count, first_used, last_used
        — ordered by total_tokens descending.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT model,
                   SUM(total_tokens)          AS total_tokens,
                   SUM(prompt_tokens)         AS prompt_tokens,
                   SUM(completion_tokens)     AS completion_tokens,
                   SUM(estimated_cost_usd)    AS estimated_cost,
                   COUNT(*)                   AS call_count,
                   CAST(MIN(created_at) AS DATE) AS first_used,
                   CAST(MAX(created_at) AS DATE) AS last_used
            FROM   ws_model_usage
            WHERE  user_id = ?
              AND  created_at >= DATEADD(DAY, ?, GETUTCDATE())
            GROUP BY model
            ORDER BY total_tokens DESC
        """, user_id, -abs(days))
        return _rows_to_dicts(cursor)


def get_daily_model_usage(user_id: int, days: int = 30) -> list[dict]:
    """Return per-day, per-model token/cost/call-count totals for a user over the trailing N days.

    Args:
        user_id: User to summarise.
        days: Lookback window in days.

    Returns:
        List of dicts (one per day+model combo) with usage_date, model,
        total_tokens, estimated_cost, call_count — newest day first.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT CAST(created_at AS DATE) AS usage_date,
                   model,
                   SUM(total_tokens)       AS total_tokens,
                   SUM(estimated_cost_usd) AS estimated_cost,
                   COUNT(*)                AS call_count
            FROM   ws_model_usage
            WHERE  user_id = ?
              AND  created_at >= DATEADD(DAY, ?, GETUTCDATE())
            GROUP BY CAST(created_at AS DATE), model
            ORDER BY usage_date DESC, model
        """, user_id, -abs(days))
        return _rows_to_dicts(cursor)


def get_all_users_model_usage(days: int = 30) -> list[dict]:
    """Return per-user, per-model token/cost/call-count totals over the trailing N days (admin view).

    Args:
        days: Lookback window in days.

    Returns:
        List of dicts (one per user+model combo) with username, user_id,
        model, total_tokens, estimated_cost, call_count — ordered by
        total_tokens descending.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.username, u.id AS user_id,
                   m.model,
                   SUM(m.total_tokens)       AS total_tokens,
                   SUM(m.estimated_cost_usd) AS estimated_cost,
                   COUNT(*)                  AS call_count
            FROM   ws_model_usage m
            JOIN   users u ON u.id = m.user_id
            WHERE  m.created_at >= DATEADD(DAY, ?, GETUTCDATE())
            GROUP BY u.username, u.id, m.model
            ORDER BY total_tokens DESC
        """, -abs(days))
        return _rows_to_dicts(cursor)


# ─────────────────────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────────────────────

def get_workspace_analytics(user_id: int, days: int = 30) -> dict:
    """Compute a summary analytics dict for a user over the trailing N days.

    Runs several aggregate queries (counts, sums, averages) within a single
    connection and assembles the results into one dict.

    Args:
        user_id: User to summarise.
        days: Lookback window in days.

    Returns:
        Dict with keys: total_conversations, total_messages, total_tokens,
        total_artifacts, avg_latency_ms, daily_usage (list of
        ``{day, msg_count}``), top_models (list of
        ``{model, tokens, calls}``, top 5 by tokens).
    """
    with _get_db() as conn:
        cursor = conn.cursor()

        # Total conversations
        cursor.execute("""
            SELECT COUNT(*) FROM ws_conversations
            WHERE user_id = ? AND created_at >= DATEADD(DAY, ?, GETUTCDATE())
        """, user_id, -abs(days))
        total_conversations = cursor.fetchone()[0]

        # Total messages
        cursor.execute("""
            SELECT COUNT(*) FROM ws_messages m
            JOIN ws_conversations c ON c.id = m.conversation_id
            WHERE c.user_id = ? AND m.created_at >= DATEADD(DAY, ?, GETUTCDATE())
              AND m.role = 'user'
        """, user_id, -abs(days))
        total_messages = cursor.fetchone()[0]

        # Total tokens
        cursor.execute("""
            SELECT ISNULL(SUM(total_tokens), 0) FROM ws_model_usage
            WHERE user_id = ? AND created_at >= DATEADD(DAY, ?, GETUTCDATE())
        """, user_id, -abs(days))
        total_tokens = cursor.fetchone()[0]

        # Total artifacts
        cursor.execute("""
            SELECT COUNT(*) FROM ws_artifacts
            WHERE user_id = ? AND created_at >= DATEADD(DAY, ?, GETUTCDATE())
        """, user_id, -abs(days))
        total_artifacts = cursor.fetchone()[0]

        # Avg latency
        cursor.execute("""
            SELECT ISNULL(AVG(m.latency_ms), 0)
            FROM   ws_messages m
            JOIN   ws_conversations c ON c.id = m.conversation_id
            WHERE  c.user_id = ? AND m.role = 'assistant'
              AND  m.created_at >= DATEADD(DAY, ?, GETUTCDATE())
        """, user_id, -abs(days))
        avg_latency = cursor.fetchone()[0]

        # Daily message counts
        cursor.execute("""
            SELECT CAST(m.created_at AS DATE) AS day,
                   COUNT(*) AS msg_count
            FROM   ws_messages m
            JOIN   ws_conversations c ON c.id = m.conversation_id
            WHERE  c.user_id = ? AND m.role = 'user'
              AND  m.created_at >= DATEADD(DAY, ?, GETUTCDATE())
            GROUP BY CAST(m.created_at AS DATE)
            ORDER BY day ASC
        """, user_id, -abs(days))
        daily_usage = _rows_to_dicts(cursor)

        # Top models
        cursor.execute("""
            SELECT TOP 5 model, SUM(total_tokens) AS tokens, COUNT(*) AS calls
            FROM ws_model_usage
            WHERE user_id = ? AND created_at >= DATEADD(DAY, ?, GETUTCDATE())
            GROUP BY model ORDER BY tokens DESC
        """, user_id, -abs(days))
        top_models = _rows_to_dicts(cursor)

    return {
        "total_conversations": total_conversations,
        "total_messages": total_messages,
        "total_tokens": total_tokens,
        "total_artifacts": total_artifacts,
        "avg_latency_ms": avg_latency,
        "daily_usage": daily_usage,
        "top_models": top_models,
    }


# ─────────────────────────────────────────────────────────────
# USER SETTINGS
# ─────────────────────────────────────────────────────────────

def get_user_settings(user_id: int) -> dict:
    """Return a user's workspace settings as a flat ``{setting_key: setting_value}`` dict."""
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT setting_key, setting_value FROM ws_user_settings WHERE user_id = ?
        """, user_id)
        return {row[0]: row[1] for row in cursor.fetchall()}


def save_user_setting(user_id: int, key: str, value: str) -> None:
    """Upsert a single workspace setting for a user (one row per user per key).

    Args:
        user_id: Owning user id.
        key: Setting key (e.g. "default_model").
        value: Setting value, stored as text.
    """
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            IF EXISTS (SELECT 1 FROM ws_user_settings WHERE user_id = ? AND setting_key = ?)
                UPDATE ws_user_settings SET setting_value = ?, updated_at = GETUTCDATE()
                WHERE user_id = ? AND setting_key = ?
            ELSE
                INSERT INTO ws_user_settings (user_id, setting_key, setting_value)
                VALUES (?, ?, ?)
        """, user_id, key, value, user_id, key, user_id, key, value)
        conn.commit()
