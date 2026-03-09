"""
reportgenerator.py  –  LLM-powered SSRS-style report config generator
=========================================================================
Generates a rich JSON config for the front-end reportlayout.html
from any pandas DataFrame, via llm_providers (OpenAI or Amazon Bedrock,
per DEFAULT_LLM_PROVIDER/REPORT_MODEL). Falls back gracefully to a
rule-based config if the LLM call fails.

Token tracking
--------------
`generate_report_config` now returns a (config, tokens_used) tuple,
mirroring the exact pattern used by `generate_dashboard_config` in
dashboard_generator.py.  The Flask route in app.py calls
`token_limits.record_usage(...)` with the returned token count,
just like the /api/bi-agents/generate-dashboard route does.

Token count is derived from prompt + completion character lengths
divided by 4 (same heuristic used across the rest of the codebase).
When the LLM call is skipped (fallback path) tokens_used = 0.
"""

import json
import re
from logging_config import get_logger
import pandas as pd
from dotenv import load_dotenv
from generator_utils import clean_nan as _safe_json, provider_chat
from llm_providers.errors import LLMConfigError

load_dotenv()
logger = get_logger(__name__)

# ── LLM availability check ────────────────────────────────────────────────────
# provider_chat() resolves its provider lazily per call (so DEFAULT_LLM_PROVIDER
# can change without restarting), but we still want a fast, cheap "is an LLM
# even configured" check at import time to preserve the original fallback
# behavior below instead of failing on the first real request.
try:
    from llm_providers.factory import get_provider as _get_provider
    _get_provider()  # raises LLMConfigError if the default provider is unconfigured
    _llm_available = True
except LLMConfigError as _e:
    _llm_available = False
    logger.warning(f"LLM provider not available: {_e}")

from llm_providers.models import resolve_default_model

_MODEL = resolve_default_model("REPORT_MODEL", tier="fast")   # fast + cheap tier for the configured provider


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_tokens(*texts: str) -> int:
    """1 token ≈ 4 chars – same heuristic used everywhere in this codebase."""
    return max(1, sum(max(0, len(t or "") // 4) for t in texts))


def _is_numeric(series: pd.Series) -> bool:
    """Returns True if the column's pandas dtype is numeric."""
    return pd.api.types.is_numeric_dtype(series)


def _is_date(series: pd.Series) -> bool:
    """Returns True if the column is a datetime dtype or looks like date strings.

    For object-dtype columns, samples up to 30 non-null values and checks
    whether at least 70% match a loose `D[-/]D[-/]D` numeric-date pattern
    (e.g. "2024-01-15", "1/15/2024").

    Args:
        series: The column to test.

    Returns:
        True if the column is datetime64 dtype, or if >=70% of a 30-row
        string sample matches the date-like pattern.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if series.dtype == object:
        sample = series.dropna().head(30).astype(str)
        hits = sample.str.match(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}").sum()
        return hits >= len(sample) * 0.7
    return False


def _column_meta(df: pd.DataFrame) -> list[dict]:
    """Classifies every column as numeric/date/categorical with sample values.

    Args:
        df: Source DataFrame.

    Returns:
        A list of `{"name", "kind", "nunique", "sample"}` dicts — one per
        column, in column order. `kind` is "numeric", "date", or
        "categorical" (per `_is_numeric`/`_is_date`). `sample` holds up to
        6 unique non-null values, coerced to str/int/float/bool.
    """
    meta = []
    for col in df.columns:
        s = df[col]
        if _is_numeric(s):
            kind = "numeric"
        elif _is_date(s):
            kind = "date"
        else:
            kind = "categorical"

        nunique     = int(s.nunique(dropna=True))
        sample_vals = s.dropna().unique().tolist()[:6]
        sample_vals = [
            str(v) if not isinstance(v, (str, int, float, bool)) else v
            for v in sample_vals
        ]
        meta.append({"name": col, "kind": kind, "nunique": nunique, "sample": sample_vals})
    return meta


def _build_fallback_config(df: pd.DataFrame, meta: list[dict]) -> dict:
    """
    Pure rule-based config – used when OpenAI is unavailable or times out.
    Produces a sensible report for any DataFrame, zero tokens consumed.
    """
    categorical = [m for m in meta if m["kind"] == "categorical"]
    numeric     = [m for m in meta if m["kind"] == "numeric"]
    date_cols   = [m for m in meta if m["kind"] == "date"]

    # Filters: categorical columns with a useful cardinality range
    filters = []
    for m in categorical:
        if 2 <= m["nunique"] <= 50:
            filters.append({
                "id":          f"filter-{m['name']}",
                "label":       m["name"].replace("_", " ").title(),
                "column_name": m["name"],
                "type":        "select",
                "options":     ["All"] + [str(v) for v in m["sample"]],
            })
        if len(filters) >= 3:
            break

    # Table columns
    columns = []
    for m in meta:
        if m["kind"] == "numeric":
            agg, fmt = "SUM",   "number"
        elif m["kind"] == "date":
            agg, fmt = "GROUP", "date"
        else:
            agg, fmt = "GROUP", "text"
        columns.append({
            "column_name": m["name"],
            "label":       m["name"].replace("_", " ").title(),
            "aggregation": agg,
            "format":      fmt,
            "sortable":    True,
        })

    group_by = [c["column_name"] for c in columns if c["aggregation"] == "GROUP"][:3]
    sort_col = (
        numeric[0]["name"]   if numeric   else
        date_cols[0]["name"] if date_cols else
        df.columns[0]
    )

    # KPI cards
    kpis = [{"label": "Total Records", "column": "__count__", "aggregation": "COUNT"}]
    for m in numeric[:3]:
        kpis.append({
            "label":       f"Total {m['name'].replace('_', ' ').title()}",
            "column":      m["name"],
            "aggregation": "SUM",
            "format":      "number",
        })

    return {
        "header": {
            "title":    "Data Report",
            "subtitle": f"{len(df):,} rows × {len(df.columns)} columns",
        },
        "filters":  filters,
        "kpis":     kpis,
        "insights": [],
        "tables": [{
            "id":       "table1",
            "title":    "Data Table",
            "columns":  columns,
            "group_by": group_by,
            "sort_by":  {"column_name": sort_col, "order": "DESC"},
            "page_size": 50,
        }],
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM call  –  returns (config_dict, tokens_used, input_tokens, output_tokens)
# ─────────────────────────────────────────────────────────────────────────────

def _llm_config(df: pd.DataFrame, meta: list[dict], fallback: dict) -> tuple:
    """
    Ask GPT-4o-mini to produce a richer report config (header, filters,
    KPIs, table column formatting/sort, insights) by improving on the
    rule-based `fallback` config.

    Args:
        df: Source DataFrame (first 15 rows sent as a CSV sample).
        meta: Column metadata list from `_column_meta(df)`.
        fallback: Rule-based config from `_build_fallback_config()`, sent
            to the LLM as a starter config to improve rather than replace.

    Returns
    -------
    (config, tokens_used, input_tokens, output_tokens)
        Falls back to `(fallback, 0, 0, 0)` if the OpenAI client is
        unavailable, the call fails, or the response isn't valid JSON.
    """
    if not _llm_available:
        logger.warning("LLM provider not available – using fallback report config.")
        return fallback, 0, 0, 0

    col_descriptions = "\n".join(
        f"  - {m['name']} [{m['kind']}]  distinct={m['nunique']}  sample={m['sample']}"
        for m in meta
    )
    csv_sample   = df.head(15).to_csv(index=False)
    schema_hint  = json.dumps(fallback, indent=2)

    system_msg = (
        "You are a senior BI analyst. Your job is to produce a JSON config "
        "that drives an SSRS-style HTML report. "
        "Return ONLY valid JSON – no markdown, no explanation."
    )

    user_msg = f"""Analyse this dataset and return an improved report config JSON.

## Column metadata
{col_descriptions}

## Sample data (first 15 rows, CSV)
{csv_sample}

## Starter config (improve it – do NOT change the top-level key names)
{schema_hint}

### Rules
1. `header.title`    – concise, meaningful title derived from the column names.
2. `header.subtitle` – one short sentence describing what the data shows.
3. `filters`         – keep up to 3 of the most useful categorical filters.
4. `kpis`            – up to 4 KPI cards; choose the most business-relevant numeric columns.
5. `tables[0].columns` – improve labels (Title Case, no underscores), set correct `format`
   values: "currency" for money, "percent" for %, "date" for dates, "number" for other
   numerics, "text" for categoricals.
6. `tables[0].sort_by` – sort by the most meaningful numeric column DESC.
7. `insights`        – list of 2–3 plain-English observations about the data.
8. Return nothing outside the JSON object.""".strip()

    try:
        result = provider_chat(
            system=system_msg,
            user_prompt=user_msg,
            model=_MODEL,
            temperature=0.2,
            max_tokens=1800,
            response_format={"type": "json_object"},
        )

        completion = (result.content or "").strip()
        # Strip accidental fences (shouldn't happen with json_object mode)
        completion = re.sub(r"^```(?:json)?\s*|\s*```$", "", completion)

        cfg = json.loads(completion)

        # ── Token count ──────────────────────────────────────────────────────
        if result.usage.total_tokens > 0:
            input_tokens  = result.usage.prompt_tokens
            output_tokens = result.usage.completion_tokens
            tokens_used   = result.usage.total_tokens
        else:
            input_tokens  = 0
            output_tokens = 0
            tokens_used   = _estimate_tokens(system_msg, user_msg, completion)

        logger.info(
            f"[report] LLM config generated | model={_MODEL} "
            f"rows={len(df)} cols={len(df.columns)} tokens={tokens_used} in={input_tokens} out={output_tokens}"
        )
        return cfg, tokens_used, input_tokens, output_tokens

    except json.JSONDecodeError as e:
        logger.warning(f"[report] JSON parse error from LLM: {e} – falling back.")
        return fallback, 0, 0, 0
    except Exception as e:
        logger.warning(f"[report] OpenAI call failed: {e} – falling back.")
        return fallback, 0, 0, 0


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_report_config(df: pd.DataFrame) -> tuple:
    """
    Generate an SSRS-style report config from a DataFrame.

    Returns
    -------
    (config, tokens_used, input_tokens, output_tokens)
    """
    if df is None or df.empty:
        empty_cfg = {
            "header":   {"title": "Empty Report", "subtitle": "No data returned."},
            "filters":  [],
            "kpis":     [],
            "tables":   [],
            "insights": [],
        }
        return empty_cfg, 0, 0, 0

    # Sanitise column names
    df.columns = [str(c).strip() for c in df.columns]

    meta     = _column_meta(df)
    fallback = _build_fallback_config(df, meta)
    config, tokens_used, input_tokens, output_tokens = _llm_config(df, meta, fallback)

    return _safe_json(config), tokens_used, input_tokens, output_tokens


def get_report_data_with_filters(
    df: pd.DataFrame,
    filters: dict | None = None,
) -> dict:
    """
    Apply simple equality filters and return serialisable row data.

    Parameters
    ----------
    df      : source DataFrame
    filters : {column_name: value} – None / "" / "All" values are ignored

    Returns
    -------
    {"columns": [...], "rows": [...]}
    """
    filtered = df.copy()

    if filters:
        for col, val in filters.items():
            if col in filtered.columns and val not in (None, "", "All"):
                filtered = filtered[filtered[col].astype(str) == str(val)]

    filtered = filtered.where(pd.notnull(filtered), None)

    return {
        "columns": filtered.columns.tolist(),
        "rows":    _safe_json(filtered.to_dict("records")),
    }