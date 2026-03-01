"""
bi_conversations_db.py — Persistent conversation storage for BI agents.

Mirrors the hub_conversations / hub_messages pattern used by hub agents,
but for the BI (NLQ) chat modal.  All functions are safe to call from
request threads; they open their own short-lived connections.
"""

import json
import uuid
from logging_config import get_logger
from app_db import get_app_db

logger = get_logger(__name__)

_MAX_DATA_ROWS = 100   # cap rows stored in data_json per message


# ── helpers ───────────────────────────────────────────────────────────────────

def _rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    result = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        _iso(d)
        result.append(d)
    return result


def _row_to_dict(cur, row) -> dict | None:
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    d = dict(zip(cols, row))
    _iso(d)
    return d


def _iso(d: dict) -> None:
    for k in ('created_at', 'updated_at'):
        if d.get(k) and hasattr(d[k], 'isoformat'):
            d[k] = d[k].isoformat()


# ── write ops ─────────────────────────────────────────────────────────────────

def create_conversation(agent_name: str, user_id: int, title: str = 'New Conversation') -> str:
    cid = str(uuid.uuid4())
    try:
        with get_app_db() as conn:
            conn.cursor().execute(
                'INSERT INTO bi_conversations (id, agent_name, user_id, title) VALUES (?, ?, ?, ?)',
                cid, agent_name[:255], user_id, (title or 'New Conversation')[:500],
            )
            conn.commit()
    except Exception as exc:
        logger.error('bi_conversations_db.create_conversation: %s', exc)
    return cid


def update_timestamp(cid: str) -> None:
    try:
        with get_app_db() as conn:
            conn.cursor().execute(
                'UPDATE bi_conversations SET updated_at=GETUTCDATE() WHERE id=?', cid
            )
            conn.commit()
    except Exception as exc:
        logger.error('bi_conversations_db.update_timestamp: %s', exc)


def save_message(
    cid: str,
    role: str,
    content: str,
    *,
    sql_query: str = '',
    tokens_used: int | None = None,
) -> None:
    try:
        with get_app_db() as conn:
            conn.cursor().execute(
                '''INSERT INTO bi_messages
                       (conversation_id, role, content, sql_query, tokens_used)
                   VALUES (?, ?, ?, ?, ?)''',
                cid,
                role[:20],
                content or '',
                (sql_query or '')[:8000],
                tokens_used,
            )
            conn.commit()
    except Exception as exc:
        logger.error('bi_conversations_db.save_message: %s', exc)


# ── read ops ──────────────────────────────────────────────────────────────────

def list_conversations(
    user_id: int,
    agent_name: str = '',
    is_admin: bool = False,
    limit: int = 30,
) -> list[dict]:
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            top = f'TOP {int(limit)}'
            msg_sub = '(SELECT COUNT(*) FROM bi_messages m WHERE m.conversation_id=c.id) AS message_count'
            if is_admin:
                if agent_name:
                    cur.execute(
                        f'SELECT {top} c.*, {msg_sub} FROM bi_conversations c '
                        f'WHERE c.agent_name=? ORDER BY c.updated_at DESC',
                        agent_name,
                    )
                else:
                    cur.execute(
                        f'SELECT {top} c.*, {msg_sub} FROM bi_conversations c '
                        f'ORDER BY c.updated_at DESC'
                    )
            else:
                if agent_name:
                    cur.execute(
                        f'SELECT {top} c.*, {msg_sub} FROM bi_conversations c '
                        f'WHERE c.user_id=? AND c.agent_name=? ORDER BY c.updated_at DESC',
                        user_id, agent_name,
                    )
                else:
                    cur.execute(
                        f'SELECT {top} c.*, {msg_sub} FROM bi_conversations c '
                        f'WHERE c.user_id=? ORDER BY c.updated_at DESC',
                        user_id,
                    )
            return _rows_to_dicts(cur)
    except Exception as exc:
        logger.error('bi_conversations_db.list_conversations: %s', exc)
        return []


def get_conversation(cid: str, user_id: int | None = None, is_admin: bool = False) -> dict | None:
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute('SELECT * FROM bi_conversations WHERE id=?', cid)
            row = cur.fetchone()
            if not row:
                return None
            c = _row_to_dict(cur, row)

            if not is_admin and user_id is not None and c.get('user_id') != user_id:
                return None  # forbidden

            cur.execute(
                'SELECT * FROM bi_messages WHERE conversation_id=? ORDER BY created_at', cid
            )
            msgs = _rows_to_dicts(cur)
            c['messages'] = msgs
            c['artifacts'] = _get_artifacts(cur, cid)
            return c
    except Exception as exc:
        logger.error('bi_conversations_db.get_conversation: %s', exc)
        return None


# ── generated artifact persistence (dashboard/report/infographic) ──────────────
# So reopening a conversation can restore the last-generated output instead of
# re-calling the LLM — see templates/components/modals.html's *Cache objects,
# which this mirrors server-side.

_ARTIFACT_TYPES = ('dashboard', 'infographic', 'report')


def save_artifact(cid: str, artifact_type: str, cache_key: str, payload: dict) -> None:
    """Persist (or overwrite) the most recent generated artifact of `artifact_type`
    for this conversation. No-ops silently on failure — persistence is a
    convenience, not something that should ever break generation itself."""
    if artifact_type not in _ARTIFACT_TYPES:
        return
    try:
        with get_app_db() as conn:
            conn.cursor().execute(
                '''MERGE bi_generated_artifacts AS t
                       USING (SELECT ? AS conversation_id, ? AS artifact_type) AS s
                       ON t.conversation_id = s.conversation_id AND t.artifact_type = s.artifact_type
                   WHEN MATCHED THEN
                       UPDATE SET cache_key=?, payload_json=?, updated_at=GETUTCDATE()
                   WHEN NOT MATCHED THEN
                       INSERT (conversation_id, artifact_type, cache_key, payload_json)
                       VALUES (?, ?, ?, ?);''',
                cid, artifact_type,
                cache_key, json.dumps(payload, default=str),
                cid, artifact_type, cache_key, json.dumps(payload, default=str),
            )
            conn.commit()
    except Exception as exc:
        logger.error('bi_conversations_db.save_artifact (%s): %s', artifact_type, exc)


def _get_artifacts(cur, cid: str) -> dict:
    """Internal: fetch all stored artifacts for a conversation using an already-open cursor."""
    cur.execute(
        'SELECT artifact_type, cache_key, payload_json FROM bi_generated_artifacts WHERE conversation_id=?',
        cid,
    )
    out: dict = {}
    for artifact_type, cache_key, payload_json in cur.fetchall():
        try:
            out[artifact_type] = {'cache_key': cache_key, 'payload': json.loads(payload_json)}
        except (TypeError, ValueError):
            continue
    return out


def get_artifacts(cid: str) -> dict:
    """Return `{artifact_type: {cache_key, payload}}` for every artifact stored
    against this conversation. Empty dict on failure or if none exist."""
    try:
        with get_app_db() as conn:
            return _get_artifacts(conn.cursor(), cid)
    except Exception as exc:
        logger.error('bi_conversations_db.get_artifacts: %s', exc)
        return {}


def delete_conversation(cid: str, user_id: int | None = None, is_admin: bool = False) -> bool:
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            if not is_admin and user_id is not None:
                cur.execute('SELECT user_id FROM bi_conversations WHERE id=?', cid)
                row = cur.fetchone()
                if row and row[0] != user_id:
                    return False  # forbidden
            cur.execute('DELETE FROM bi_messages WHERE conversation_id=?', cid)
            cur.execute('DELETE FROM bi_conversations WHERE id=?', cid)
            conn.commit()
            return True
    except Exception as exc:
        logger.error('bi_conversations_db.delete_conversation: %s', exc)
        return False
