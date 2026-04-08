"""Lazy-singleton loaders and token-recording helpers used only by
scheduler_service.py's job execution pipeline (_execute_job) — extracted
in Phase 3 Slice 6.
"""
import os
import threading
from typing import Dict, List, Optional, Callable

from logging_config import get_logger

logger = get_logger(__name__)

# ── lazy module refs ───────────────────────────────────────────────────────────
_nlq_engine      = None
_agent_manager   = None
_dashboard_gen   = None
_report_gen      = None
_infographic_gen = None
_ppt_gen         = None
_token_limits    = None

# Single lock guards all lazy-singleton initializations.
_init_lock = threading.Lock()


# ── lazy loaders ───────────────────────────────────────────────────────────────

def _get_token_limits():
    """Lazily import and cache the `token_limits` module (avoids import cycles at startup)."""
    global _token_limits
    if _token_limits is None:
        with _init_lock:
            if _token_limits is None:
                import token_limits as _tl
                _token_limits = _tl
    return _token_limits


def _load_clients() -> Dict[str, Dict]:
    """Return all configured clients, keyed by client id (via `clients_db.load_clients`)."""
    from clients_db import load_clients as _load_clients_db
    return _load_clients_db()


def _detect_client(rows: List[Dict]) -> Optional[Dict]:
    """Guess which configured client a job's result rows belong to, for PPT branding.

    Scans each row's ``MeetingTitle`` for a client id or a >3-char word from
    the client's name. Used only by the ``ppt`` tool branch of `_execute_job`
    to choose a branding config when the job's delivery settings don't
    already specify one.

    Args:
        rows: Query result rows (dicts), expected to optionally contain a
            ``MeetingTitle`` field.

    Returns:
        The matching client config dict, or ``None`` if no client matched
        (a warning is logged in that case).
    """
    clients = _load_clients()
    if not clients:
        return None
    titles = " ".join((r.get("MeetingTitle") or "").lower() for r in rows)
    for client in clients.values():
        cid   = client.get("id",   "").lower()
        cname = client.get("name", "").lower()
        if cid and cid in titles:
            return client
        for word in cname.replace("&", " ").replace(",", " ").replace("-", " ").split():
            if len(word) > 3 and word in titles:
                return client
    logger.warning("  Client not detected — PPT uses empty config")
    return None


def _get_nlq_engine():
    """Lazily construct and cache the shared `NLQEngine` instance used for scheduled jobs."""
    global _nlq_engine
    if _nlq_engine is None:
        with _init_lock:
            if _nlq_engine is None:
                from nlq_engine       import NLQEngine
                from database_manager import db_manager
                _nlq_engine = NLQEngine(
                    openai_api_key=os.getenv("OPENAI_API_KEY"),
                    database_manager=db_manager,
                )
    return _nlq_engine


def _get_agent_manager():
    """Lazily import and cache the shared `agent_manager` singleton."""
    global _agent_manager
    if _agent_manager is None:
        with _init_lock:
            if _agent_manager is None:
                from agent_manager import agent_manager as am
                _agent_manager = am
    return _agent_manager


def _get_dashboard_gen():
    """Lazily import and cache `dashboard_generator.generate_dashboard_config`."""
    global _dashboard_gen
    if _dashboard_gen is None:
        with _init_lock:
            if _dashboard_gen is None:
                from dashboard_generator import generate_dashboard_config
                _dashboard_gen = generate_dashboard_config
    return _dashboard_gen


def _get_report_gen():
    """Lazily import and cache `reportgenerator.generate_report_config`."""
    global _report_gen
    if _report_gen is None:
        with _init_lock:
            if _report_gen is None:
                from reportgenerator import generate_report_config
                _report_gen = generate_report_config
    return _report_gen


def _get_infographic_gen():
    """Lazily construct and cache the shared `InfographicGenerator` instance."""
    global _infographic_gen
    if _infographic_gen is None:
        with _init_lock:
            if _infographic_gen is None:
                from infographicgenerator import InfographicGenerator
                _infographic_gen = InfographicGenerator()
    return _infographic_gen


def _get_ppt_gen():
    """Lazily construct and cache the shared `PresentationGenerator` instance."""
    global _ppt_gen
    if _ppt_gen is None:
        with _init_lock:
            if _ppt_gen is None:
                from ppt_generator import PresentationGenerator
                _ppt_gen = PresentationGenerator()
    return _ppt_gen


# ── Token recording helpers ────────────────────────────────────────────────────

def _make_user_context(job: Dict) -> Dict:
    """
    Build a user_context dict from the job's creator fields.
    This is passed to nlq_engine.process_question() so the engine's
    internal token_recorder writes against the right user.

    If created_by is 0 / missing, fall back to admin (no recording).
    """
    uid   = job.get("created_by", 0)
    uname = job.get("created_by_username", "scheduler")
    role  = job.get("created_by_role", "user")   # see note below

    if not uid:
        # No real user attached — use admin context so tokens are skipped
        return {"id": 0, "username": "scheduler", "role": "admin"}

    return {"id": uid, "username": uname, "role": role}


def _make_token_recorder(user_context: Dict, agent_name: str,
                          question: str) -> Callable:
    """
    Returns a callable(call_type, tokens, input_tokens=0, output_tokens=0)
    that writes to token_usage. No-op when user is admin or has no real id.
    """
    tl  = _get_token_limits()
    uid = user_context.get("id", 0)

    if not uid or user_context.get("role") == "admin":
        return lambda ct, t, in_tok=0, out_tok=0, model="": None

    def _record(call_type: str, tokens: int,
                input_tokens: int = 0, output_tokens: int = 0,
                model: str = "") -> None:
        try:
            tl.record_usage(
                user_id       = uid,
                tokens        = tokens,
                call_type     = call_type,
                agent_name    = agent_name,
                question      = question[:200],
                input_tokens  = input_tokens,
                output_tokens = output_tokens,
                model         = model,
            )
            logger.info(
                f"[token/job] user={user_context.get('username')} "
                f"type={call_type} tokens={tokens} in={input_tokens} out={output_tokens} agent={agent_name}"
            )
        except Exception as e:
            logger.warning(f"[token/job] record_usage failed: {e}")

    return _record


def _estimate_tokens(*texts: str) -> int:
    """4 chars ≈ 1 token (same formula as token_limits.estimate_tokens)."""
    return max(1, sum(max(0, len(t or "") // 4) for t in texts))
