"""
clients_db.py — SQL Server-backed client configuration store.

Replaces clients.json file I/O. All three original callers
(app.py, ppt_generator.py, scheduler_service.py) import from here.

Azure migration: table lives in SQL Server → Azure SQL Database.
No code changes needed on migration — only the connection string changes.
"""
import json
from logging_config import get_logger
import threading
from typing import Dict, List, Optional

from app_db import get_app_db

logger = get_logger(__name__)

# In-memory cache so hot paths (ppt_generator, scheduler) don't hit SQL on
# every call. Invalidated on every write.
_cache: Optional[Dict[str, Dict]] = None
_cache_lock = threading.Lock()


def _invalidate():
    """Drop the in-memory client cache so the next load_clients() call re-reads SQL Server."""
    global _cache
    with _cache_lock:
        _cache = None


def load_clients() -> Dict[str, Dict]:
    """Return {client_id: client_dict} from cache or SQL Server."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, email_db_config, all_our_services, ppt_history, extra_data
                FROM app_clients ORDER BY name
            """)
            rows = cur.fetchall()
        result: Dict[str, Dict] = {}
        for row in rows:
            cid, name, email_cfg, services, history, extra = row
            client: Dict = {
                "id":               cid,
                "name":             name,
                "email_db_config":  _safe_json(email_cfg, {}),
                "all_our_services": _safe_json(services, []),
                "ppt_history":      _safe_json(history, []),
            }
            client.update(_safe_json(extra, {}))
            result[cid] = client
        with _cache_lock:
            _cache = result
        return result
    except Exception as exc:
        logger.warning("clients_db.load_clients failed: %s", exc)
        return {}


def list_clients() -> List[Dict]:
    """Return all clients as a list (order follows load_clients(), i.e. by name)."""
    return list(load_clients().values())


def get_client(client_id: str) -> Optional[Dict]:
    """Return a single client dict by id, or None if not found."""
    return load_clients().get(client_id)


def save_client(client: Dict) -> bool:
    """Upsert a client record."""
    cid  = client.get("id", "")
    if not cid:
        return False
    known = {"id", "name", "email_db_config", "all_our_services", "ppt_history"}
    extra = {k: v for k, v in client.items() if k not in known}
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                MERGE app_clients AS t
                USING (SELECT ? AS id) AS s ON t.id = s.id
                WHEN MATCHED THEN UPDATE SET
                    name             = ?,
                    email_db_config  = ?,
                    all_our_services = ?,
                    ppt_history      = ?,
                    extra_data       = ?,
                    updated_at       = GETUTCDATE()
                WHEN NOT MATCHED THEN INSERT
                    (id, name, email_db_config, all_our_services, ppt_history, extra_data)
                    VALUES (?, ?, ?, ?, ?, ?);
            """,
                cid,
                client.get("name", ""),
                json.dumps(client.get("email_db_config") or {}),
                json.dumps(client.get("all_our_services") or []),
                json.dumps(client.get("ppt_history") or []),
                json.dumps(extra),
                cid,
                client.get("name", ""),
                json.dumps(client.get("email_db_config") or {}),
                json.dumps(client.get("all_our_services") or []),
                json.dumps(client.get("ppt_history") or []),
                json.dumps(extra))
            conn.commit()
        _invalidate()
        return True
    except Exception as exc:
        logger.warning("clients_db.save_client failed: %s", exc)
        return False


def delete_client(client_id: str) -> bool:
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM app_clients WHERE id = ?", client_id)
            conn.commit()
        _invalidate()
        return True
    except Exception as exc:
        logger.warning("clients_db.delete_client failed: %s", exc)
        return False


def update_ppt_history(client_id: str, entry: Dict) -> None:
    """Prepend a PPT history entry (max 3 kept), thread-safe."""
    with _cache_lock:
        pass  # ensure no concurrent cache read while we write
    client = get_client(client_id)
    if client is None:
        return
    history: List[Dict] = list(client.get("ppt_history") or [])
    history.insert(0, entry)
    client["ppt_history"] = history[:3]
    save_client(client)


def _safe_json(value, default):
    """Parse value as JSON if it's a string; pass dicts/lists through; return default on None/parse failure."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default
