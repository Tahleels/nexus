# token_limits.py
# ─────────────────────────────────────────────────────────────────────────────
# Daily token budget enforcement — tracks EVERY LLM call, not just chat.
#
# Pricing
# ───────
# Live pricing is fetched from LiteLLM's community-maintained JSON once per
# day and cached in memory.  URL:
#   https://raw.githubusercontent.com/BerriAI/litellm/main/
#   model_prices_and_context_window.json
#
# On fetch failure (network down, rate-limited, etc.) the local fallback dict
# is used so the app never breaks.  Re-fetch is attempted on next restart or
# after the 24-hour TTL expires.
#
# Limits (tokens / day)
# ─────────────────────
#   admin  → 1 000 000
#   dev    → 1 000 000
#   user   → 1 000 000
# ─────────────────────────────────────────────────────────────────────────────
"""token_limits.py — Daily per-user token quota tracking and enforcement.

Every LLM call site in the app (chat, dashboard/report/infographic/PPT
generation, schema analysis, hub chat, workflows, ...) is expected to call
either record_tokens()/record_usage() after the call, and check_limit() /
check_and_record() before it, so that ``token_usage`` (SQL Server, accessed
via ``auth._get_db()``) is the single ledger for both quota enforcement and
the cost-estimate dashboards in app.py and the admin token-quota page.

Pricing is resolved per model via get_model_pricing()/compute_cost() using a
live LiteLLM price list (refreshed at most once per 24h) with a local
fallback table for offline/rate-limited operation.
"""

import json
import time
import urllib.request
from logging_config import get_logger
import auth

logger = get_logger(__name__)

# ── Daily budget limits ────────────────────────────────────────────────────────
DAILY_LIMITS: dict = {
    "admin": 1_000_000,
    "dev":   1_000_000,
    "user":  1_000_000,
}

# ── Live pricing (litellm public JSON) ─────────────────────────────────────────
_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_PRICING_TTL       = 86_400          # 24 hours
_pricing_cache     = None            # type: dict | None
_pricing_fetched   = 0.0             # epoch seconds


def _load_pricing() -> dict:
    """Return live pricing dict; refreshed at most once per 24 h."""
    global _pricing_cache, _pricing_fetched
    now = time.time()
    if _pricing_cache is not None and (now - _pricing_fetched) < _PRICING_TTL:
        return _pricing_cache
    try:
        req = urllib.request.Request(
            _LITELLM_PRICING_URL,
            headers={"User-Agent": "nexus/pricing-check"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        _pricing_cache   = data
        _pricing_fetched = now
        logger.info("[pricing] Refreshed live model pricing from LiteLLM")
        return data
    except Exception as exc:
        logger.warning("[pricing] Live fetch failed (%s) — using fallback", exc)
        return _pricing_cache or {}  # stale cache or empty


# ── Fallback pricing (used when live fetch is unavailable) ────────────────────
# Keys are lowercase model name prefixes.
# Values: (input_cost_per_token, output_cost_per_token)
_FALLBACK: dict = {
    # OpenAI
    "gpt-4o-mini":              (0.00000015,  0.00000060),
    "gpt-4o":                   (0.00000250,  0.00001000),
    "gpt-4":                    (0.00003000,  0.00006000),
    "gpt-3.5-turbo":            (0.00000050,  0.00000150),
    # Anthropic
    "claude-3-5-haiku":         (0.00000080,  0.00000400),
    "claude-haiku-4-5":         (0.00000080,  0.00000400),
    "claude-3-5-sonnet":        (0.00000300,  0.00001500),
    "claude-3-sonnet":          (0.00000300,  0.00001500),
    "claude-sonnet-4-5":        (0.00000300,  0.00001500),
    "claude-sonnet-4-6":        (0.00000300,  0.00001500),
    "claude-3-haiku":           (0.00000025,  0.00000125),
    "claude-3-opus":            (0.00001500,  0.00007500),
    "claude-opus-4-8":          (0.00001500,  0.00007500),
}
_DEFAULT_INPUT_RATE  = 0.00000250   # gpt-4o input
_DEFAULT_OUTPUT_RATE = 0.00001000   # gpt-4o output

# ── Legacy blended rate (kept for backwards-compat; prefer compute_cost) ──────
COST_PER_1M = 4.75   # USD — gpt-4o blended (70 % in / 30 % out)


def get_model_pricing(model: str) -> tuple:
    """
    Return (input_cost_per_token, output_cost_per_token) for *model*.
    1. Tries live LiteLLM pricing (exact match).
    2. Tries live LiteLLM pricing (prefix match).
    3. Falls back to _FALLBACK dict.
    4. Returns gpt-4o defaults.
    """
    m     = (model or "gpt-4o").strip().lower()
    live  = _load_pricing()

    # Exact match in live data
    entry = live.get(m) or live.get(model or "")
    if isinstance(entry, dict) and "input_cost_per_token" in entry:
        return (float(entry["input_cost_per_token"]),
                float(entry["output_cost_per_token"]))

    # Prefix match in live data — the queried name starts with a known base key.
    # e.g. "gpt-4o-2024-11-20" starts with "gpt-4o". Intentionally NOT reversed:
    # k.startswith(m) would wrongly match "gpt-4o-mini" for a "gpt-4o" query.
    for key, val in live.items():
        if not isinstance(val, dict) or "input_cost_per_token" not in val:
            continue
        k = key.lower()
        if m.startswith(k):
            return (float(val["input_cost_per_token"]),
                    float(val["output_cost_per_token"]))

    # Fallback dict (same direction only)
    for key, rates in _FALLBACK.items():
        if m.startswith(key):
            return rates

    return (_DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE)


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return USD cost for the given token counts using exact per-direction rates."""
    in_rate, out_rate = get_model_pricing(model)
    return (input_tokens * in_rate) + (output_tokens * out_rate)


def model_cost_rate(model: str) -> float:
    """Blended per-token rate (70 % input / 30 % output). Backwards-compat."""
    in_rate, out_rate = get_model_pricing(model)
    return in_rate * 0.70 + out_rate * 0.30


CALL_TYPES = {
    "chat", "analysis", "schema_analysis", "schema_prune", "insight",
    "ppt_generate", "ppt_optimize", "dashboard", "report",
    "workspace_chat",
    "hub_chat", "hub_workflow",
}


def estimate_tokens(*texts: str) -> int:
    """Fallback estimation: 1 token ≈ 4 characters."""
    return max(1, sum(max(0, len(t or "") // 4) for t in texts))


def get_today_usage(user_id: int) -> int:
    with auth._get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ISNULL(SUM(tokens_used), 0)
            FROM   token_usage
            WHERE  user_id = ?
              AND  used_at >= CAST(GETUTCDATE() AS DATE)
        """, user_id)
        row = cursor.fetchone()
        return int(row[0]) if row else 0


def get_today_cost(user_id: int) -> float:
    """Return actual USD cost for today by applying per-direction rates to stored I/O counts.

    Grouped by the model each call actually ran on, so a user whose calls
    span e.g. gpt-4o-mini and gpt-4o gets a real blended total instead of
    every row being priced as if it were gpt-4o.
    """
    with auth._get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ISNULL(model, ''),
                   ISNULL(SUM(tokens_used),   0),
                   ISNULL(SUM(input_tokens),  0),
                   ISNULL(SUM(output_tokens), 0)
            FROM   token_usage
            WHERE  user_id = ?
              AND  used_at >= CAST(GETUTCDATE() AS DATE)
            GROUP BY model
        """, user_id)
        rows = cursor.fetchall()
    total_cost = 0.0
    for model, total_toks, in_toks, out_toks in rows:
        total_toks, in_toks, out_toks = int(total_toks), int(in_toks), int(out_toks)
        if in_toks or out_toks:
            # model is '' for legacy rows recorded before model tracking was
            # added — gpt-4o remains the best available guess for those only.
            total_cost += compute_cost(model or "gpt-4o", in_toks, out_toks)
        else:
            # No I/O split recorded at all — blended fallback.
            total_cost += total_toks * (COST_PER_1M / 1_000_000)
    return total_cost


def record_usage(user_id, tokens, call_type="chat", agent_name="", question="",
                 input_tokens=0, output_tokens=0, model="", agent_id=""):
    try:
        with auth._get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO token_usage
                    (user_id, tokens_used, call_type, agent_name, question,
                     input_tokens, output_tokens, model, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                user_id,
                tokens,
                (call_type  or "chat")[:50],
                (agent_name or "")[:255],
                (question   or "")[:1000],
                input_tokens  or 0,
                output_tokens or 0,
                (model or "")[:100] or None,
                (agent_id or "")[:36] or None,
            )
            conn.commit()
    except Exception as e:
        logger.error("token_limits.record_usage failed (%s): %s", call_type, e)


def get_user_quota(user) -> int | None:
    """Return the effective daily token limit for a user.

    Checks users.token_quota first; falls back to the role-based DAILY_LIMITS dict.
    Returns None only if the role has no configured limit (unlimited).
    """
    custom = user.get("token_quota")
    if custom is not None:
        return int(custom)
    role = user.get("role", "user")
    return DAILY_LIMITS.get(role)


def check_limit(user):
    limit = get_user_quota(user)
    if limit is None:
        return True, "", 0, None
    used = get_today_usage(user["id"])
    if used >= limit:
        msg = (
            f"Daily token limit reached ({used:,} / {limit:,} tokens used). "
            f"Your limit resets at midnight UTC."
        )
        return False, msg, used, limit
    return True, "", used, limit


def check_and_record(user, question="", agent_name=""):
    allowed, msg, _, _ = check_limit(user)
    return allowed, msg


def record_tokens(user, call_type="chat", agent_name="", question="",
                  *, prompt="", response="", extra="",
                  actual_tokens: int = None,
                  input_tokens: int = 0, output_tokens: int = 0,
                  model: str = "", agent_id: str = ""):
    """
    Record token usage.  Pass actual_tokens + input_tokens + output_tokens from
    the LLM API response when available; falls back to character estimation.
    Pass model (the exact model id the call ran on) so cost can be computed
    with the correct per-model rate instead of an assumed default. Pass
    agent_id (e.g. a hub_agents.id) when available so downstream analytics
    can join on a stable ID instead of the mutable agent_name string.
    """
    role  = user.get("role", "user")
    limit = get_user_quota(user)
    if limit is None:
        return 0
    tokens = actual_tokens if (actual_tokens and actual_tokens > 0) \
             else estimate_tokens(question, prompt, response, extra)
    record_usage(user_id=user["id"], tokens=tokens, call_type=call_type,
                 agent_name=agent_name, question=question,
                 input_tokens=input_tokens, output_tokens=output_tokens,
                 model=model, agent_id=agent_id)
    logger.info(
        "[token] user=%s role=%s type=%s tokens=%d%s agent=%s in=%d out=%d",
        user.get("username", user["id"]), role, call_type, tokens,
        "(actual)" if actual_tokens else "(est)",
        agent_name or "-", input_tokens, output_tokens,
    )
    return tokens


def get_usage_summary(user):
    role  = user.get("role", "user")
    limit = get_user_quota(user)
    if limit is None:
        return {"role": role, "used_today": 0, "limit": None,
                "remaining": None, "unlimited": True,
                "cost_used": 0, "cost_limit": None}
    used      = get_today_usage(user["id"])
    remaining = max(0, limit - used)
    cost_used = get_today_cost(user["id"])
    rate = COST_PER_1M / 1_000_000
    return {
        "role": role, "used_today": used, "limit": limit,
        "remaining": remaining, "unlimited": False,
        "custom_quota": user.get("token_quota") is not None,
        "cost_used":      round(cost_used,       4),
        "cost_limit":     round(limit * rate,    2),
        "cost_remaining": round(max(0.0, round(limit * rate, 4) - cost_used), 4),
    }


def get_detailed_usage(user_id, days=7):
    with auth._get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT CAST(used_at AS DATE) AS usage_date,
                   call_type,
                   SUM(tokens_used)   AS tokens,
                   SUM(input_tokens)  AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   COUNT(*)           AS requests
            FROM  token_usage
            WHERE user_id = ?
              AND used_at >= DATEADD(DAY, ?, GETUTCDATE())
            GROUP BY CAST(used_at AS DATE), call_type
            ORDER BY usage_date DESC, call_type
        """, user_id, -abs(days))
        cols = [d[0] for d in cursor.description]
        return [
            {k: (str(v) if hasattr(v, "isoformat") else v) for k, v in zip(cols, row)}
            for row in cursor.fetchall()
        ]


def get_all_users_usage_today():
    with auth._get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.username, r.name AS role,
                   u.token_quota,
                   ISNULL(SUM(t.tokens_used), 0) AS used_today
            FROM  users u
            JOIN  roles r ON r.id = u.role_id
            LEFT JOIN token_usage t
                   ON t.user_id = u.id
                  AND t.used_at >= CAST(GETUTCDATE() AS DATE)
            GROUP BY u.id, u.username, r.name, u.token_quota
            ORDER BY used_today DESC
        """)
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    for row in rows:
        custom = row.get("token_quota")
        limit  = int(custom) if custom is not None else DAILY_LIMITS.get(row["role"])
        row["limit"]        = limit
        row["custom_quota"] = custom is not None
        row["unlimited"]    = limit is None
        row["pct_used"]     = (round(row["used_today"] / limit * 100, 1)
                               if limit else None)
    return rows
