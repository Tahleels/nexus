"""
email_intelligence.py — Nexus AI
Thin data-access layer for the dbo.SalesEmailIntelligence SQL Server table.

All public functions return plain Python dicts/lists — no ORM, no heavy deps.
Connection is opened and closed per-call (connection pool handled by the driver).
"""

import os
from logging_config import get_logger
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pyodbc

logger = get_logger(__name__)

# ── Connection config ──────────────────────────────────────────────────────────
# No hardcoded server/credentials — all sourced from env vars, empty by default.
# Set EMAIL_DB_SERVER / EMAIL_DB_DATABASE / EMAIL_DB_USERNAME / EMAIL_DB_PASSWORD
# (or call configure() at startup) to enable this feature.
_DB_CFG = {
    "server":   os.getenv("EMAIL_DB_SERVER", ""),
    "port":     os.getenv("EMAIL_DB_PORT", "1433"),
    "database": os.getenv("EMAIL_DB_DATABASE", ""),
    "username": os.getenv("EMAIL_DB_USERNAME", ""),
    "password": os.getenv("EMAIL_DB_PASSWORD", ""),
    "driver":   os.getenv("EMAIL_DB_DRIVER", "ODBC Driver 17 for SQL Server"),
}

TABLE = "dbo.SalesEmailIntelligence"


def configure(password: str = "", server: str = "", database: str = "") -> None:
    """Override defaults at app startup — call from app.py after load_dotenv().

    Args:
        password: New value for ``_DB_CFG["password"]``; ignored if empty.
        server: New value for ``_DB_CFG["server"]``; ignored if empty.
        database: New value for ``_DB_CFG["database"]``; ignored if empty.
    """
    if password:  _DB_CFG["password"] = password
    if server:    _DB_CFG["server"]   = server
    if database:  _DB_CFG["database"] = database


def _connect_with(cfg: dict) -> pyodbc.Connection:
    """Open a new autocommit pyodbc connection using the given config dict.

    Args:
        cfg: A ``_DB_CFG``-shaped dict with server/port/database/username/
            password/driver keys. If ``cfg["password"]`` is falsy, falls back
            to the ``EMAIL_DB_PASSWORD`` environment variable.

    Returns:
        An open ``pyodbc.Connection`` with ``autocommit=True``.
    """
    pwd = cfg.get("password") or os.getenv("EMAIL_DB_PASSWORD", "")
    dsn = (
        f"DRIVER={{{cfg['driver']}}};"
        f"SERVER={cfg['server']},{cfg['port']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['username']};"
        f"PWD={pwd};"
        "TrustServerCertificate=yes;"
        "Connection Timeout=8;"
    )
    return pyodbc.connect(dsn, autocommit=True)


def _connect() -> pyodbc.Connection:
    """Open a new pyodbc connection using the module-level ``_DB_CFG`` defaults."""
    return _connect_with(_DB_CFG)


def resolve_client_db_cfg(email_db_config: dict, db_manager=None) -> dict:
    """Resolve a client's email_db_config to a _DB_CFG-style dict.

    mode == "connection": look up named connection via db_manager.get_connection()
    mode == "custom":     use the fields from email_db_config directly

    Args:
        email_db_config: Client-specific config dict with a ``"mode"`` key of
            ``"connection"`` or ``"custom"``, plus mode-specific fields
            (``connection_name`` for "connection"; ``server``/``port``/
            ``database``/``username``/``password``/``driver`` for "custom").
        db_manager: Object exposing ``get_connection(name)`` used to resolve a
            named connection when ``mode == "connection"``. Required for that
            mode; ignored otherwise.

    Returns:
        A ``_DB_CFG``-shaped dict, or ``{}`` to signal the caller should fall
        back to the module-level ``_DB_CFG`` defaults (e.g. on missing/unknown
        mode, missing db_manager, or a lookup failure).
    """
    if not email_db_config:
        return {}
    mode = email_db_config.get("mode", "")
    if mode == "connection":
        conn_name = email_db_config.get("connection_name", "")
        if not conn_name or db_manager is None:
            return {}
        try:
            conn = db_manager.get_connection(conn_name)
            if not conn:
                logger.warning(f"resolve_client_db_cfg: connection '{conn_name}' not found")
                return {}
            return {
                "server":   conn.get("server", _DB_CFG["server"]),
                "port":     conn.get("port", _DB_CFG["port"]),
                "database": conn.get("db_name", _DB_CFG["database"]),
                "username": conn.get("username", _DB_CFG["username"]),
                "password": conn.get("password", _DB_CFG["password"]),
                "driver":   _DB_CFG["driver"],
            }
        except Exception as e:
            logger.warning(f"resolve_client_db_cfg: could not load connection '{conn_name}': {e}")
            return {}
    if mode == "custom":
        return {
            "server":   email_db_config.get("server") or _DB_CFG["server"],
            "port":     email_db_config.get("port") or _DB_CFG["port"],
            "database": email_db_config.get("database") or _DB_CFG["database"],
            "username": email_db_config.get("username") or _DB_CFG["username"],
            "password": email_db_config.get("password") or _DB_CFG["password"],
            "driver":   email_db_config.get("driver") or _DB_CFG["driver"],
        }
    return {}


def _rows_to_dicts(cursor) -> List[Dict]:
    """Convert all remaining rows of a pyodbc cursor into a list of column-name dicts."""
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ── Keyword helpers ────────────────────────────────────────────────────────────

_GENERIC_WORDS = {
    "and", "the", "of", "for", "inc", "llc", "ltd", "co", "corp",
    "group", "associates", "partners", "solutions", "services", "global",
    "international", "consulting", "technology", "technologies",
}


def _build_client_keywords(client_name: str, client_id: str = "") -> List[str]:
    """Build all search terms to try against the Clients column.

    For "Ariela & Associates" / id "aai" produces:
      ["Ariela & Associates", "aai", "Ariela", "AA", "A&A"]

    Strategy:
    1. Full display name
    2. Client ID (short key stored in clients.json, e.g. "aai")
    3. Every non-generic word >= 3 chars from the name
    4. Acronym from ALL words >= 1 char (catches "AA" from "Ariela Associates")
    5. Acronym from ALL words joined with "&" separator (catches "A&A")

    Args:
        client_name: Full display name of the client.
        client_id: Short client key (e.g. as stored in clients.json), optional.

    Returns:
        A deduplicated (case-insensitive), order-preserving list of keyword
        strings to use in ``LIKE`` clauses against the ``Clients`` column.
    """
    raw: List[str] = []
    if client_name:
        raw.append(client_name)
    if client_id:
        raw.append(client_id)

    parts = [p for p in re.split(r"[\s&,.\-/\\]+", client_name) if p]

    # Individual non-generic words >= 3 chars
    for p in parts:
        if len(p) >= 3 and p.lower() not in _GENERIC_WORDS:
            raw.append(p)

    # Acronym from ALL parts (including short/generic) — at least 2 words required
    if len(parts) >= 2:
        acronym_plain = "".join(w[0].upper() for w in parts)   # e.g. "AA"
        acronym_amp   = "&".join(w[0].upper() for w in parts)   # e.g. "A&A"
        raw.append(acronym_plain)
        raw.append(acronym_amp)

    # Deduplicate preserving order, case-insensitive
    seen: set = set()
    keywords: List[str] = []
    for k in raw:
        k = k.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            keywords.append(k)

    return keywords


# ── Public API ─────────────────────────────────────────────────────────────────

def get_senders_for_client(client_name: str, db_cfg: dict = None,
                           client_id: str = "") -> List[str]:
    """Return unique SenderName values for a given client.

    Tries every keyword derived from client_name + client_id so that
    abbreviations like 'AAI', short names like 'Ariela', or the full
    display name all match.

    Args:
        client_name: Full display name of the client to search for.
        db_cfg: Optional ``_DB_CFG``-shaped dict to connect with instead of
            the module defaults (e.g. from ``resolve_client_db_cfg``).
        client_id: Optional short client key included in the keyword search.

    Returns:
        Sorted list of distinct sender names found in the
        ``SalesEmailIntelligence`` table, or ``[]`` on no match or error.
    """
    keywords = _build_client_keywords(client_name, client_id)
    if not keywords:
        return []
    try:
        conn   = _connect_with(db_cfg) if db_cfg else _connect()
        cursor = conn.cursor()
        or_clause  = " OR ".join("Clients LIKE ?" for _ in keywords)
        params     = [f"%{k}%" for k in keywords]
        cursor.execute(f"""
            SELECT DISTINCT SenderName
            FROM   {TABLE}
            WHERE  ({or_clause})
              AND  SenderName IS NOT NULL
              AND  SenderName <> ''
            ORDER  BY SenderName
        """, *params)
        senders = [row[0] for row in cursor.fetchall()]
        conn.close()
        logger.info(
            f"📧 Found {len(senders)} senders for client '{client_name}' "
            f"(keywords: {keywords})"
        )
        return senders
    except Exception as e:
        logger.warning(f"get_senders_for_client failed: {e}")
        return []


def get_emails(
    client_name: str,
    sender_names: List[str],
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    max_emails: int = 30,
    db_cfg: dict = None,
    client_id: str = "",
) -> List[Dict]:
    """Fetch emails for a client + selected senders within an optional date range.

    Args:
        client_name: Full display name of the client (used to build keyword
            filters against the ``Clients`` column).
        sender_names: Sender names to restrict results to (from
            ``get_senders_for_client``). Returns ``[]`` immediately if empty.
        date_from: Inclusive lower bound as ``'YYYY-MM-DD'``, or ``None`` for
            no lower bound.
        date_to: Inclusive upper bound as ``'YYYY-MM-DD'``, or ``None`` for
            no upper bound.
        max_emails: Maximum number of rows to return (used in a ``TOP`` clause).
        db_cfg: Optional ``_DB_CFG``-shaped dict to connect with instead of
            the module defaults.
        client_id: Optional short client key included in the keyword search.

    Returns:
        List of dicts with keys: Id, Subject, Body (truncated to 2000 chars),
        SenderEmail, SenderName, CreatedAt, Clients — newest first. Empty list
        on no match or error.
    """
    if not sender_names:
        return []

    keywords = _build_client_keywords(client_name, client_id)
    if not keywords:
        return []

    try:
        conn   = _connect_with(db_cfg) if db_cfg else _connect()
        cursor = conn.cursor()

        sender_placeholders = ", ".join("?" * len(sender_names))
        client_or_clause    = " OR ".join("Clients LIKE ?" for _ in keywords)

        params: list = [f"%{k}%" for k in keywords] + list(sender_names)
        date_clause = ""
        if date_from:
            date_clause += " AND CreatedAt >= ?"
            params.append(date_from + " 00:00:00")
        if date_to:
            date_clause += " AND CreatedAt <= ?"
            params.append(date_to + " 23:59:59")

        cursor.execute(f"""
            SELECT TOP {max_emails}
                   Id, Subject,
                   LEFT(Body, 2000) AS Body,
                   SenderEmail, SenderName,
                   CreatedAt, Clients
            FROM   {TABLE}
            WHERE  ({client_or_clause})
              AND  SenderName  IN ({sender_placeholders})
              {date_clause}
            ORDER  BY CreatedAt DESC
        """, *params)

        rows = _rows_to_dicts(cursor)
        conn.close()
        logger.info(f"📧 Fetched {len(rows)} emails for client '{client_name}' (keywords: {keywords})")
        return rows
    except Exception as e:
        logger.warning(f"get_emails failed: {e}")
        return []


def parse_date_range_from_question(question: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract a date range from free-text questions.

    Handles patterns:
      - "last N days / weeks / months"
      - "this week / this month"
      - "since YYYY-MM-DD"
      - "from YYYY-MM-DD to YYYY-MM-DD"
      - explicit ISO dates anywhere in text

    Args:
        question: Free-text user question to scan for date-range phrases.

    Returns:
        Tuple of ``(date_from, date_to)`` as ``'YYYY-MM-DD'`` strings, with
        either or both being ``None`` when no date signal is found.
    """
    today = datetime.utcnow().date()
    q     = question.lower()

    # ── "last N days/weeks/months" ──────────────────────────────────────────
    m = re.search(r"last\s+(\d+)\s+(day|week|month)s?", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit == "day" else \
                timedelta(weeks=n) if unit == "week" else \
                timedelta(days=n * 30)
        return str(today - delta), str(today)

    # ── "this week" ──────────────────────────────────────────────────────────
    if "this week" in q:
        monday = today - timedelta(days=today.weekday())
        return str(monday), str(today)

    # ── "this month" ─────────────────────────────────────────────────────────
    if "this month" in q:
        first = today.replace(day=1)
        return str(first), str(today)

    # ── "since YYYY-MM-DD" ───────────────────────────────────────────────────
    m = re.search(r"since\s+(\d{4}-\d{2}-\d{2})", q)
    if m:
        return m.group(1), str(today)

    # ── "from DATE to DATE" ──────────────────────────────────────────────────
    m = re.search(
        r"from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", q
    )
    if m:
        return m.group(1), m.group(2)

    # ── bare ISO date anywhere ────────────────────────────────────────────────
    dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", question)
    if len(dates) == 2:
        return min(dates), max(dates)
    if len(dates) == 1:
        return dates[0], str(today)

    # ── no date signal → no filter ───────────────────────────────────────────
    return None, None


def format_emails_for_llm(emails: List[Dict]) -> str:
    """Convert a list of email dicts into a compact text block for the LLM prompt.

    Truncates long bodies; keeps Subject + short Body for context.

    Args:
        emails: List of email dicts as returned by ``get_emails`` (keys:
            Subject, Body, SenderName/SenderEmail, CreatedAt).

    Returns:
        A formatted multi-line string ready to inject into an LLM prompt, or
        ``""`` if ``emails`` is empty.
    """
    if not emails:
        return ""
    lines = [f"EMAIL INTELLIGENCE ({len(emails)} emails):"]
    for i, e in enumerate(emails, 1):
        subject = str(e.get("Subject") or "").strip()[:120]
        body    = str(e.get("Body") or "").strip()[:600]
        sender  = e.get("SenderName") or e.get("SenderEmail") or "Unknown"
        date    = str(e.get("CreatedAt") or "")[:10]
        lines.append(
            f"\n[Email {i}] {date} | From: {sender}\n"
            f"Subject: {subject}\n"
            f"{body}"
        )
        if i < len(emails):
            lines.append("\n─────")
    return "\n".join(lines)