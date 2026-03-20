"""db_exec.py — Shared "run SQL, return list-of-dicts / dict-or-None" helper.

This exact wrapper logic was independently reimplemented at least three
times in the codebase: agents_hub_bp.py::_ss_exec, agents/core/tools/
registry.py::_hub_db_query, and (in a slightly different shape)
database/org_db.py::_exec_safe. All three ultimately go through
app_db.get_app_db() underneath. This module is the one canonical copy;
callers keep their existing local alias name (e.g. `_ss_exec`) so call
sites don't need to change.
"""
from logging_config import get_logger
from app_db import get_app_db

logger = get_logger(__name__)


def run_query(sql, *params, fetchall=False, fetchone=False):
    """Run a query against the app SQL Server database.

    commit() is deferred until AFTER any fetch so the ODBC driver does not
    discard the result set early (HY010 Function sequence error).
    """
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            if params:
                cur.execute(sql, *params)
            else:
                cur.execute(sql)

            if fetchall:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

            if fetchone:
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))

            conn.commit()
    except Exception as e:
        logger.error(f"[db_exec] SQL error: {e}")
        return [] if fetchall else None
