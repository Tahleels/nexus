"""Data-analysis tool implementations: read-only SQL querying, the single
write-capable watchlist tool, and local CSV/Excel exploration.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime
from _shared import _FILE_DIR, _hub_db_query

FETCH_ROW_CAP = 5000  # safety ceiling on rows pulled from the DB cursor — not a display/LLM limit


def query_database(sql: str, connection_name: str = None, **kwargs) -> dict:
    """Execute a read-only SQL query against a configured database connection.

    Looks up connections registered in Source > Database Connections (via
    ``database_manager.DatabaseConnectionManager``), connects using the
    matching driver for the connection's ``type`` (mssql/postgresql/mysql/
    sqlite), runs the query, and returns up to ``FETCH_ROW_CAP`` rows (the
    full match set for any reasonably-sized query; a safety ceiling, not a
    display limit — the caller decides how many of these rows actually go
    to the LLM vs. the UI). Only statements starting with a read-only keyword
    are permitted (enforced by a simple
    prefix check, not a full SQL parser); this is a security-relevant
    guard and must not be relaxed.

    Args:
        sql (str): SQL query to execute. Must start with one of: select,
            show, describe, explain, with, pragma (case-insensitive).
        connection_name (str, optional): Name of the configured connection
            to use. If omitted, the first available connection is used.
        **kwargs: Orchestrator-injected context (unused directly by this
            tool beyond the documented parameters).

    Returns:
        dict: On success, ``{"success": True, "connection": str,
        "db_type": str, "columns": list[str], "rows": list[list],
        "row_count": int, "row_count_capped": bool, "sql": str}`` (row
        values are made JSON-safe — datetimes are ISO-formatted strings,
        other non-primitive types are stringified; ``row_count_capped`` is
        True only if the match count hit ``FETCH_ROW_CAP``, meaning the
        true total may be higher). On failure, ``{"error": str, "sql": str, ...}`` —
        e.g. when the query doesn't start with a safe keyword, no
        connections are configured, the named connection isn't found, the
        database type is unsupported, no ODBC driver is found (mssql), or
        the required DB driver package isn't installed.
    """
    safe_starters = ('select', 'show', 'describe', 'explain', 'with', 'pragma')
    if not sql.strip().lower().startswith(safe_starters):
        return {"error": "Only SELECT / read-only queries are permitted.", "sql": sql}

    try:
        from database_manager import DatabaseConnectionManager
        mgr         = DatabaseConnectionManager()
        connections = mgr.load_connections()

        if not connections:
            return {
                "error": "No database connections are configured. "
                         "Add one in Source > Database Connections.",
                "sql": sql
            }

        # Pick specified or first available connection
        if connection_name:
            cfg = next((c for c in connections if c['name'] == connection_name), None)
            if not cfg:
                available = [c['name'] for c in connections]
                return {"error": f"Connection '{connection_name}' not found. "
                                 f"Available: {available}", "sql": sql}
        else:
            cfg = connections[0]

        db_type  = cfg.get('type', '').lower()
        server   = cfg.get('server', '')
        port     = cfg.get('port', '')
        database = cfg.get('database', '')
        username = cfg.get('username', '')
        password = cfg.get('password', '')

        def _rows_to_serializable(rows):
            """Convert DB cursor rows to JSON-safe lists (ISO dates, stringified non-primitives)."""
            out = []
            for row in rows:
                out.append([
                    v.isoformat() if hasattr(v, 'isoformat')
                    else (str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v)
                    for v in row
                ])
            return out

        if db_type == 'mssql':
            import pyodbc
            # Try multiple ODBC driver versions
            for drv in ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server',
                        'SQL Server']:
                try:
                    cs = (f"DRIVER={{{drv}}};SERVER={server},{port or 1433};"
                          f"DATABASE={database};UID={username};PWD={password};"
                          f"TrustServerCertificate=yes")
                    conn = pyodbc.connect(cs, timeout=15)
                    break
                except Exception:
                    continue
            else:
                return {"error": "No suitable ODBC Driver found for SQL Server.", "sql": sql}
            with conn:
                cur = conn.cursor()
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = _rows_to_serializable(cur.fetchmany(FETCH_ROW_CAP))

        elif db_type == 'postgresql':
            import psycopg2
            with psycopg2.connect(
                    host=server, port=int(port or 5432),
                    dbname=database, user=username, password=password,
                    connect_timeout=15) as conn:
                cur = conn.cursor()
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = _rows_to_serializable(cur.fetchmany(FETCH_ROW_CAP))

        elif db_type == 'mysql':
            import pymysql
            with pymysql.connect(
                    host=server, port=int(port or 3306),
                    db=database, user=username, password=password,
                    connect_timeout=15) as conn:
                cur = conn.cursor()
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = _rows_to_serializable(cur.fetchmany(FETCH_ROW_CAP))

        elif db_type == 'sqlite':
            import sqlite3
            with sqlite3.connect(database, timeout=15) as conn:
                cur = conn.cursor()
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = _rows_to_serializable(cur.fetchmany(FETCH_ROW_CAP))

        else:
            return {"error": f"Database type '{db_type}' is not supported for direct queries.",
                    "supported": ["mssql", "postgresql", "mysql", "sqlite"], "sql": sql}

        return {
            "success":       True,
            "connection":    cfg['name'],
            "db_type":       db_type,
            "columns":       cols,
            "rows":          rows,
            "row_count":     len(rows),
            "row_count_capped": len(rows) == FETCH_ROW_CAP,
            "sql":           sql,
        }

    except ImportError as e:
        return {"error": f"Required database driver not installed: {e}", "sql": sql}
    except Exception as e:
        return {"error": str(e), "sql": sql}


WATCHLIST_CONNECTION_NAME = "Sales & Leads"


def update_company_watchlist(action: str, new_client: str, reference_company: str = None,
                              connection_name: str = None, **kwargs) -> dict:
    """Add or remove a company from [News].[CompanyNames] (the company watchlist).

    This is the ONLY table Hub Agents may write to, and the ONLY write
    operation exposed to chat agents — this is a deliberate security
    boundary and must not be widened. ``action='add'`` reactivates the row
    (sets ``is_removed='no'``) if ``new_client`` already exists, otherwise
    INSERTs a new row with the given ``reference_company``.
    ``action='remove'`` soft-deletes by setting ``is_removed='yes'``
    (matched on ``new_client``, and additionally on ``reference_company``
    if provided to narrow the match). All values are passed as
    parameterized query arguments — no string-built SQL — and the
    function re-queries after the write to verify the change took effect.

    Args:
        action (str): 'add' or 'remove' (case-insensitive).
        new_client (str): Company name to add or remove. Required.
        reference_company (str, optional): Reference client name (typically
            looked up from lead tables by the calling agent). Defaults to
            'None' (the literal string) when not provided. On 'remove', if
            provided and not 'None', narrows the match to this reference too.
        connection_name (str, optional): Name of the database connection to
            use. Must resolve to an 'mssql' connection. Defaults to the
            "Sales & Leads" connection (WATCHLIST_CONNECTION_NAME) if omitted
            — never falls back to the first configured connection, since the
            watchlist table only exists on that specific server.
        **kwargs: Orchestrator-injected context (unused directly).

    Returns:
        dict: On success or partial failure, ``{"success": bool, "action":
        str, "new_client": str, "reference_company": str, "verification":
        list[dict], "message": str}`` where ``verification`` is the result
        of re-querying the row after the write and ``success`` reflects
        whether that re-query confirmed the expected state. On hard
        failure, ``{"error": str}`` — e.g. invalid ``action``, missing
        ``new_client``, no connections configured, named connection not
        found, connection type isn't mssql, no ODBC driver found, or the
        pyodbc driver isn't installed.
    """
    action = (action or '').strip().lower()
    if action not in ('add', 'remove'):
        return {"error": "action must be 'add' or 'remove'"}

    new_client = (new_client or '').strip()
    if not new_client:
        return {"error": "new_client is required"}

    reference_company = (reference_company or 'None').strip() or 'None'

    try:
        from database_manager import DatabaseConnectionManager
        mgr = DatabaseConnectionManager()
        connections = mgr.load_connections()

        if not connections:
            return {"error": "No database connections are configured. "
                             "Add one in Source > Database Connections."}

        # The company watchlist always lives on the "Sales & Leads" connection —
        # never fall back to the first configured connection, since that can
        # silently point at an unrelated server/database.
        connection_name = connection_name or WATCHLIST_CONNECTION_NAME
        cfg = next((c for c in connections if c['name'] == connection_name), None)
        if not cfg:
            available = [c['name'] for c in connections]
            return {"error": f"Connection '{connection_name}' not found. "
                             f"Available: {available}"}

        if cfg.get('type', '').lower() != 'mssql':
            return {"error": "update_company_watchlist currently supports SQL Server connections only."}

        import pyodbc
        for drv in ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server', 'SQL Server']:
            try:
                cs = (f"DRIVER={{{drv}}};SERVER={cfg['server']},{cfg.get('port') or 1433};"
                      f"DATABASE={cfg['database']};UID={cfg['username']};PWD={cfg['password']};"
                      f"TrustServerCertificate=yes")
                conn = pyodbc.connect(cs, timeout=15)
                break
            except Exception:
                continue
        else:
            return {"error": "No suitable ODBC Driver found for SQL Server."}

        with conn:
            cur = conn.cursor()

            if action == 'add':
                cur.execute(
                    "SELECT 1 FROM [News].[CompanyNames] WHERE new_client = ?",
                    new_client,
                )
                exists = cur.fetchone() is not None
                if exists:
                    cur.execute(
                        "UPDATE [News].[CompanyNames] SET is_removed = 'no' WHERE new_client = ?",
                        new_client,
                    )
                else:
                    cur.execute(
                        "INSERT INTO [News].[CompanyNames] (new_client, reference_company, is_removed) "
                        "VALUES (?, ?, 'no')",
                        new_client, reference_company,
                    )
                conn.commit()
                cur.execute(
                    "SELECT new_client, reference_company, is_removed FROM [News].[CompanyNames] "
                    "WHERE new_client = ? AND is_removed = 'no'",
                    new_client,
                )
            else:  # remove
                if reference_company and reference_company != 'None':
                    cur.execute(
                        "UPDATE [News].[CompanyNames] SET is_removed = 'yes' "
                        "WHERE new_client = ? AND reference_company = ?",
                        new_client, reference_company,
                    )
                else:
                    cur.execute(
                        "UPDATE [News].[CompanyNames] SET is_removed = 'yes' WHERE new_client = ?",
                        new_client,
                    )
                conn.commit()
                cur.execute(
                    "SELECT new_client, reference_company, is_removed FROM [News].[CompanyNames] "
                    "WHERE new_client = ? AND is_removed = 'yes'",
                    new_client,
                )

            verify_cols = [d[0] for d in cur.description]
            verify_rows = [dict(zip(verify_cols, row)) for row in cur.fetchall()]

        success = len(verify_rows) > 0
        if action == 'add':
            message = (f"'{new_client}' is now active (is_removed='no')." if success else
                       f"'{new_client}' was NOT found as active after the add — retry needed.")
        else:
            message = (f"'{new_client}' is now removed (is_removed='yes')." if success else
                       f"'{new_client}' was NOT found as removed after the remove — retry needed.")

        return {
            "success":           success,
            "action":            action,
            "new_client":        new_client,
            "reference_company": reference_company,
            "verification":      verify_rows,
            "message":           message,
        }

    except ImportError as e:
        return {"error": f"Required database driver not installed: {e}"}
    except Exception as e:
        return {"error": str(e)}


def analyze_csv_files(files=None, pattern=None, preview_schema=False,
                      code=None, **kwargs) -> dict:
    """Load CSV/Excel files from configured directories and inspect or analyze them.

    Two-step workflow intended for agents:

    1. Schema preview (learn column names / dtypes before writing code)::

        analyze_csv_files(pattern="GA_*.csv", preview_schema=True)

    2. Run analysis (assign the answer to a variable named ``result``)::

        analyze_csv_files(pattern="GA_*.csv",
                          code="result = df.groupby('Week')['Sessions'].sum().to_dict()")

    Matched files are loaded with pandas (``read_excel`` for .xlsx/.xls/.xlsm,
    ``read_csv`` with encoding fallback utf-8-sig -> utf-8 -> latin-1 -> cp1252
    for .csv) and exposed in the ``code`` execution scope as:

    - ``df``  — single combined DataFrame (pd.concat with a ``_source_file``
      tag column when more than one file matched).
    - ``dfs`` — dict of ``{filename: DataFrame}`` for per-file access.
    - ``pd``  — the pandas module.

    Code execution runs via ``exec()`` with real ``__builtins__`` (i.e. not
    a hardened sandbox like :func:`run_python_code`) but rejects code
    containing blocked substrings such as ``subprocess``, ``socket``,
    ``requests``, ``urllib``, ``os.system``, ``shutil``, ``open(``,
    ``exec(``, ``eval(`` — this is a security-relevant guard and must not
    be relaxed.

    Args:
        files (str | list[str], optional): Explicit filename(s) to load
            (matched against basename or full path). Takes precedence over
            ``pattern`` when provided.
        pattern (str, optional): Glob pattern to match filenames, e.g.
            ``"GA_*.csv"``. Ignored when ``files`` is given.
        preview_schema (bool): If True, skip code execution and instead
            return column names, dtypes, and up to 3 sample rows per unique
            schema found among the matched files (files sharing an
            identical column signature are grouped to avoid duplicating the
            same schema in the response). Defaults to False.
        code (str, optional): Pandas code to ``exec()`` against the loaded
            files. Must assign its answer to a variable named ``result``.
            stdout (e.g. from ``print()``) is captured and returned too. If
            omitted (and ``preview_schema`` is False), the files are loaded
            but no analysis is run.
        **kwargs: Tool-config / orchestrator-injected context. Recognized
            key: ``directories`` (list[str] or comma-separated str) — one
            or more absolute folder paths to search; each directory's
            ``archive`` subdirectory (if present) is searched too. Defaults
            to the agent file store directory when not configured.

    Returns:
        dict: For ``preview_schema=True``: ``{"success": True,
        "file_count": int, "files": list[str], "schema_groups":
        list[dict], "load_errors": dict | None, "hint": str}``. For code
        execution: ``{"success": True, "file_count": int, "files":
        list[str], "result": Any, "output": str | None, "load_errors":
        dict | None}`` where ``result`` is serialised specially for
        DataFrames (capped at 50 rows) and Series (capped at 100 entries)
        to limit token cost. On failure, ``{"error": str}`` (no files
        matched / all failed to load / pandas not installed / blocked
        operation in code) or ``{"success": False, "error": str, "code":
        str}`` if the exec() call itself raises.
    """
    import fnmatch
    import io as _io
    import contextlib as _ctx
    try:
        import pandas as pd
    except ImportError:
        return {"error": "pandas is not installed on this server."}

    # ── Resolve search directories ────────────────────────────────────────────
    raw_dirs = kwargs.get('directories', [])
    if isinstance(raw_dirs, str):
        raw_dirs = [d.strip() for d in raw_dirs.split(',') if d.strip()]
    search_dirs = [os.path.expandvars(os.path.expanduser(d)) for d in raw_dirs] or [_FILE_DIR]

    # Always include archive/ subdirectories of each configured dir
    expanded_dirs = []
    for d in search_dirs:
        expanded_dirs.append(d)
        archive = os.path.join(d, 'archive')
        if os.path.isdir(archive):
            expanded_dirs.append(archive)

    # ── Collect candidate files ───────────────────────────────────────────────
    _SUPPORTED_EXTS = {'.csv', '.xlsx', '.xls', '.xlsm'}

    def _collect(dirs, pat, explicit):
        """Scan dirs for supported CSV/Excel files matching explicit names or a glob pattern."""
        found = {}  # filename → abs path (deduplicated, last-write wins)
        for d in dirs:
            if not os.path.isdir(d):
                continue
            try:
                entries = os.listdir(d)
            except PermissionError:
                continue
            for fname in entries:
                fpath = os.path.join(d, fname)
                if not os.path.isfile(fpath):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _SUPPORTED_EXTS:
                    continue
                if explicit:
                    if fname in explicit or fpath in explicit:
                        found[fname] = fpath
                elif pat:
                    if fnmatch.fnmatch(fname, pat):
                        found[fname] = fpath
                else:
                    found[fname] = fpath
        return found

    explicit_list = files if isinstance(files, list) else ([files] if isinstance(files, str) else None)
    matched = _collect(expanded_dirs, pattern, explicit_list)

    if not matched:
        hint = (f"pattern='{pattern}'" if pattern else
                f"files={explicit_list}" if explicit_list else "no filter")
        return {"error": f"No matching files found ({hint}) in directories: {expanded_dirs}"}

    # ── Load files ────────────────────────────────────────────────────────────
    _ENCODINGS = ("utf-8-sig", "utf-8", "latin-1", "cp1252")

    def _load(fpath: str) -> pd.DataFrame:
        """Load one CSV/Excel file into a DataFrame, trying multiple text encodings for CSV."""
        ext = os.path.splitext(fpath)[1].lower()
        if ext in ('.xlsx', '.xls', '.xlsm'):
            return pd.read_excel(fpath)
        # CSV — try encodings in order
        last_exc = None
        for enc in _ENCODINGS:
            try:
                return pd.read_csv(fpath, encoding=enc, on_bad_lines='skip')
            except Exception as e:
                last_exc = e
        raise last_exc

    loaded, load_errors = {}, {}
    for fname, fpath in sorted(matched.items()):
        try:
            loaded[fname] = _load(fpath)
        except Exception as exc:
            load_errors[fname] = str(exc)

    if not loaded:
        return {"error": "All matched files failed to load.", "details": load_errors}

    # ── Schema preview ────────────────────────────────────────────────────────
    if preview_schema:
        # Group files by their column signature — avoid sending identical schemas N times
        sig_map = {}   # signature → list of filenames
        sig_info = {}  # signature → {schema, sample, row_counts}
        for fname, frame in loaded.items():
            sig = tuple(frame.columns.tolist())
            sig_map.setdefault(sig, []).append(fname)
            if sig not in sig_info:
                sig_info[sig] = {
                    "columns":    len(frame.columns),
                    "schema":     {col: str(dtype) for col, dtype in frame.dtypes.items()},
                    "sample":     frame.head(3).to_dict(orient='records'),
                    "row_counts": {},
                }
            sig_info[sig]["row_counts"][fname] = len(frame)

        # Build compact output: one schema block per unique structure
        schema_groups = []
        for sig, fnames in sig_map.items():
            info = sig_info[sig]
            schema_groups.append({
                "files":      fnames,
                "columns":    info["columns"],
                "schema":     info["schema"],
                "sample":     info["sample"],
                "row_counts": info["row_counts"],
            })

        return {
            "success":      True,
            "file_count":   len(loaded),
            "files":        list(loaded.keys()),
            "schema_groups": schema_groups,
            "load_errors":  load_errors or None,
            "hint":         (
                "Use 'df' for a combined DataFrame of all files, or "
                "'dfs[filename]' to access a specific file."
            ),
        }

    # ── Code execution ────────────────────────────────────────────────────────
    if not code:
        return {
            "success":    True,
            "file_count": len(loaded),
            "files":      list(loaded.keys()),
            "message":    "Files loaded. Pass 'code' to analyse or 'preview_schema=True' to inspect.",
            "load_errors": load_errors or None,
        }

    # Build combined df
    try:
        frames = list(loaded.values())
        if len(frames) == 1:
            df_combined = frames[0].copy()
        else:
            # Add source column so agent can distinguish files after concat
            tagged = []
            for fname, frame in loaded.items():
                f = frame.copy()
                f['_source_file'] = fname
                tagged.append(f)
            df_combined = pd.concat(tagged, ignore_index=True)
    except Exception as exc:
        return {"error": f"Failed to combine DataFrames: {exc}"}

    # Blocked patterns — no file writes, no subprocess, no network
    _BLOCKED = [
        'subprocess', 'socket', 'requests', 'urllib',
        'os.system', 'os.popen', 'os.remove', 'os.rmdir', 'shutil',
        '__import__', 'open(', 'exec(', 'eval(',
    ]
    for b in _BLOCKED:
        if b in code:
            return {"error": f"Blocked operation in code: '{b}'"}

    stdout_buf = _io.StringIO()
    local_vars = {'df': df_combined, 'dfs': loaded, 'pd': pd}
    try:
        with _ctx.redirect_stdout(stdout_buf):
            exec(code, {"__builtins__": __builtins__, "pd": pd}, local_vars)  # noqa: S102
    except Exception as exc:
        return {"success": False, "error": str(exc), "code": code}

    output   = stdout_buf.getvalue()
    result   = local_vars.get('result')

    # Serialise result — keep token cost low
    def _serialise(val):
        """Convert the user code's `result` value to a token-cheap, JSON-safe representation.

        DataFrames are capped at 50 rows and Series at 100 entries (with a
        note about how many were omitted); plain JSON-serialisable values
        pass through unchanged; anything else falls back to `str(val)`.
        """
        if val is None:
            return None
        if isinstance(val, pd.DataFrame):
            total = len(val)
            # Cap at 50 rows — for trends/aggregations the result is already small;
            # raw row dumps should be avoided in agent code anyway
            preview = val.head(50).to_dict(orient='records')
            out = {"type": "dataframe", "total_rows": total,
                   "returned_rows": len(preview), "columns": list(val.columns),
                   "data": preview}
            if total > 50:
                out["note"] = f"{total - 50} more rows not shown — use aggregation in code."
            return out
        if isinstance(val, pd.Series):
            s = val.to_dict()
            # Cap series at 100 entries
            if len(s) > 100:
                keys = list(s)[:100]
                return {"type": "series", "total": len(s), "data": {k: s[k] for k in keys},
                        "note": f"{len(s) - 100} more entries not shown."}
            return {"type": "series", "data": s}
        try:
            import json as _j
            _j.dumps(val)
            return val
        except (TypeError, ValueError):
            return str(val)

    return {
        "success":     True,
        "file_count":  len(loaded),
        "files":       list(loaded.keys()),
        "result":      _serialise(result),
        "output":      output or None,
        "load_errors": load_errors or None,
    }
