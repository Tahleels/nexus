"""Shared, low-level helpers used by more than one built-in tool module
in ``agents/core/tools/``: the project-root/file-store path constants and
the default SQL Server query helper (``_hub_db_query``).
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime

# ── Project root helpers ──────────────────────────────────────────────────────
# registry.py lives at agents/core/tools/registry.py
# Project root is 3 levels up.
_TOOLS_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_TOOLS_DIR, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Persistent storage directory.
# Set AGENT_FILE_STORE in .env to override (supports UNC paths like \\server\share\store).
# When running as a Windows service account (NSSM), the process inherits that account's
# permissions so any path the account can access will work here automatically.
_env_store = os.environ.get('AGENT_FILE_STORE', '').strip()
_STORE_DIR  = _env_store if _env_store else os.path.join(_PROJECT_ROOT, 'Data', 'agent_store')
os.makedirs(_STORE_DIR, exist_ok=True)

_FILE_DIR       = os.path.join(_STORE_DIR, 'files')
os.makedirs(_FILE_DIR, exist_ok=True)

_KNOWLEDGE_FILE = os.path.join(_STORE_DIR, 'knowledge.json')


def _hub_db_query(sql, params=None, fetchall=False, fetchone=False):
    """Run a query against the app's SQL Server via the ``auth`` module's connection helper.

    This is the default low-level DB accessor used by tool implementations
    that need to read/write hub tables (e.g. ``hub_agents``) without going
    through a configured user database connection. Callers may instead pass
    a ``_db_query`` override via kwargs (see module docstring) to substitute
    a different query function, primarily for testing.

    Args:
        sql (str): SQL statement to execute. Can be a SELECT or a
            write statement (INSERT/UPDATE/DELETE) — write statements are
            committed automatically.
        params (tuple | list | None): Positional parameters to bind to the
            query via the DB driver's parameter substitution. Defaults to
            None (no parameters).
        fetchall (bool): If True, fetch all rows and return them as a list
            of dicts (column name -> value). Defaults to False.
        fetchone (bool): If True, fetch a single row and return it as a
            dict, or None if there are no rows. Defaults to False.

    Returns:
        list[dict] | dict | None: A list of row-dicts when ``fetchall`` is
        True; a single row-dict (or None) when ``fetchone`` is True; None
        for write-only calls (neither flag set) on success. On any
        exception (including connection failure), returns ``[]`` if
        ``fetchall`` was requested, otherwise ``None`` — errors are
        swallowed rather than raised.
    """
    try:
        auth_mod = sys.modules.get('auth')
        if not auth_mod:
            import auth as auth_mod
        with auth_mod._get_db() as conn:
            cur = conn.cursor()
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            if fetchall:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            if fetchone:
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    return dict(zip(cols, row))
            conn.commit()
    except Exception as e:
        return [] if fetchall else None
