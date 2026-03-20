"""Sandboxed-execution and persistent key/value store tool implementations.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime

def run_python_code(code: str, **kwargs) -> dict:
    """Execute general-purpose Python code in a restricted sandbox and return its output.

    Unlike :func:`analyze_csv_files`, this sandbox does not have pandas or
    file/network access in scope at all — it is meant for small
    computations, string/data manipulation, and similar logic. The
    sandbox restricts execution in two layers: a denylist of dangerous
    substrings checked against the raw source text (e.g. ``subprocess``,
    ``socket``, ``requests``, ``urllib``, ``os.system``, ``shutil``,
    ``open(``, ``exec(``, ``eval(``), and a curated ``__builtins__``/
    ``__import__`` replacement that only allows a fixed module allowlist
    (math, json, re, datetime, collections, itertools, statistics, random,
    string, decimal, fractions, functools) and a fixed set of safe
    builtins. These restrictions are security-relevant and must not be
    relaxed.

    Args:
        code (str): Python source to execute. Use ``print()`` to emit
            output; any plain (non-callable, non-underscore-prefixed)
            top-level variables left in scope after execution are also
            returned.
        **kwargs: Orchestrator-injected context (unused directly by this
            tool).

    Returns:
        dict: On success, ``{"success": True, "output": str, "variables":
        dict[str, str], "code": str}`` where ``output`` is captured stdout
        and ``variables`` maps each surviving local variable name to its
        ``repr()``. On failure, ``{"success": False, "error": str, "code":
        str}`` — e.g. a blocked substring was found, a disallowed module
        was imported, or the code raised during execution.
    """
    _ALLOWED_IMPORTS = {
        'math', 'json', 're', 'datetime', 'collections',
        'itertools', 'statistics', 'random', 'string',
        'decimal', 'fractions', 'functools',
    }
    _BLOCKED_PATTERNS = [
        'subprocess', 'socket', 'requests', 'urllib',
        'os.system', 'os.popen', 'os.remove', 'os.rmdir',
        'shutil', 'open(', 'exec(', 'eval(',
    ]
    for b in _BLOCKED_PATTERNS:
        if b in code:
            return {"error": f"Blocked operation: '{b}'", "code": code}

    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Restrict imports inside the sandbox to the `_ALLOWED_IMPORTS` allowlist."""
        if name.split('.')[0] not in _ALLOWED_IMPORTS:
            raise ImportError(f"Import of '{name}' is not permitted in sandbox. "
                              f"Allowed: {sorted(_ALLOWED_IMPORTS)}")
        return __import__(name, globals, locals, fromlist, level)

    safe_builtins = {
        "__import__": _safe_import,
        "print": print, "len": len, "range": range, "list": list,
        "dict": dict, "str": str, "int": int, "float": float, "bool": bool,
        "tuple": tuple, "set": set, "frozenset": frozenset,
        "sum": sum, "max": max, "min": min, "abs": abs,
        "sorted": sorted, "reversed": reversed, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter,
        "round": round, "divmod": divmod, "pow": pow,
        "type": type, "isinstance": isinstance, "issubclass": issubclass,
        "repr": repr, "chr": chr, "ord": ord, "hex": hex, "oct": oct, "bin": bin,
        "hash": hash, "id": id, "callable": callable,
        "True": True, "False": False, "None": None,
        "Exception": Exception, "ValueError": ValueError,
        "TypeError": TypeError, "KeyError": KeyError, "IndexError": IndexError,
    }

    try:
        import io, contextlib
        stdout_buf = io.StringIO()
        local_vars = {}
        globs = {"__builtins__": safe_builtins, "__name__": "__sandbox__"}
        with contextlib.redirect_stdout(stdout_buf):
            exec(code, globs, local_vars)  # noqa: S102

        output = stdout_buf.getvalue()
        result_vars = {k: repr(v) for k, v in local_vars.items()
                       if not k.startswith('_') and not callable(v)}
        return {"success": True, "output": output,
                "variables": result_vars, "code": code}
    except Exception as e:
        return {"success": False, "error": str(e), "code": code}


def manage_knowledge(action: str, key: str, value: str = None, **kwargs) -> dict:
    """Get, set, delete, or list key/value items in the persistent ``app_knowledge_store`` table.

    Storage persists across agent sessions/conversations (it is a shared,
    app-wide table, not scoped per-agent or per-user).

    Args:
        action (str): One of ``"get"``, ``"set"``, ``"delete"``, ``"list"``.
        key (str): Key name to operate on. Required for get/set/delete;
            ignored for list.
        value (str, optional): Value to store. Used only when
            ``action="set"`` (treated as empty string if None).
        **kwargs: Orchestrator-injected context (unused directly by this
            tool).

    Returns:
        dict: Shape depends on ``action``:

        - ``"set"``: ``{"success": bool, "action": "set", "key": str}``,
          plus ``"error": str`` on failure.
        - ``"get"``: ``{"found": bool, "key": str, "value": str | None,
          "updated": str}`` when found, or ``{"found": False, "key": str,
          "value": None}`` (optionally with ``"error"``) when not found
          or on failure.
        - ``"delete"``: ``{"success": True, "action": "delete", "key":
          str}`` — always reports success even if the DB call raised
          (failures are silently swallowed).
        - ``"list"``: ``{"success": bool, "keys": list[str], "count":
          int}``, plus ``"error": str`` on failure.
        - Unknown action: ``{"error": str}``.
    """
    try:
        from app_db import get_app_db
        _db = get_app_db
    except Exception:
        _db = None

    if action == 'set':
        try:
            with _db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    MERGE app_knowledge_store AS target
                    USING (SELECT ? AS key_name) AS source ON target.key_name = source.key_name
                    WHEN MATCHED THEN
                        UPDATE SET value = ?, updated_at = GETUTCDATE()
                    WHEN NOT MATCHED THEN
                        INSERT (key_name, value) VALUES (?, ?);
                """, key, value or "", key, value or "")
                conn.commit()
            return {"success": True, "action": "set", "key": key}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "set", "key": key}

    elif action == 'get':
        try:
            with _db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT value, updated_at FROM app_knowledge_store WHERE key_name = ?", key)
                row = cursor.fetchone()
            if row is None:
                return {"found": False, "key": key, "value": None}
            return {"found": True, "key": key, "value": row[0], "updated": str(row[1])}
        except Exception as e:
            return {"found": False, "key": key, "value": None, "error": str(e)}

    elif action == 'delete':
        try:
            with _db() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM app_knowledge_store WHERE key_name = ?", key)
                conn.commit()
        except Exception:
            pass
        return {"success": True, "action": "delete", "key": key}

    elif action == 'list':
        try:
            with _db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key_name FROM app_knowledge_store ORDER BY key_name")
                keys = [r[0] for r in cursor.fetchall()]
            return {"success": True, "keys": keys, "count": len(keys)}
        except Exception as e:
            return {"success": False, "keys": [], "count": 0, "error": str(e)}

    return {"error": f"Unknown action '{action}'. Use: get, set, delete, list"}
