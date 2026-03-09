"""dashboard_generator.py — Nexus AI · Generators

Turns a pandas DataFrame of BI query results into a dashboard JSON config
(header, filters, KPI cards, charts) via an LLM call (see `llm_providers` —
OpenAI or Amazon Bedrock, per `DEFAULT_LLM_PROVIDER`/`DASHBOARD_MODEL`). This
is the module that actually performs "chart type selection" for dashboards:
the LLM picks chart types (line/doughnut/bar) and label/value column pairs
inside `get_dashboard_json_structure()`'s schema, constrained by the
pre-computed column analysis (`_analyse_filter_columns`) so it never has to
guess at date ranges or categorical values.

Public API:
    generate_dashboard_config(df) -> (config: dict | None, tokens_used: int,
                                       input_tokens: int, output_tokens: int)

The returned `config` dict is consumed by `generators/artifact_builder.py`'s
`build_dashboard_pdf()` (scheduled-job PDF export) and is also returned
directly as JSON to the front-end dashboard layout page for live rendering.
"""

import json
import pandas as pd
from dotenv import load_dotenv
from logging_config import get_logger
from generator_utils import clean_nan, provider_chat

# --- Configuration ---
load_dotenv()
logger = get_logger(__name__)

from llm_providers.models import resolve_default_model

DASHBOARD_MODEL = resolve_default_model("DASHBOARD_MODEL", tier="quality")


# --- Helper: Detect if a column is date-like ---
def _is_date_column(series: pd.Series) -> bool:
    """Return True if the column contains parseable date values."""
    # Already a datetime dtype
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    # Try parsing a sample of non-null string values
    sample = series.dropna().astype(str).head(20)
    parsed = 0
    for val in sample:
        try:
            pd.to_datetime(val, infer_datetime_format=True)
            parsed += 1
        except Exception:
            pass
    return parsed >= max(1, len(sample) * 0.6)


# --- Helper: Extract ALL unique years and months from a date column ---
def _extract_date_options(series: pd.Series) -> dict:
    """
    Returns {"years": ["2023","2024","2025"], "months": ["January","February",...]}
    using every row — not just the sample.
    """
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    parsed = pd.to_datetime(series.dropna(), infer_datetime_format=True, errors="coerce").dropna()
    years  = sorted({str(d.year) for d in parsed})
    months = [m for m in month_names if any(d.month == (month_names.index(m) + 1) for d in parsed)]
    return {"years": years, "months": months}


# --- Helper: Extract ALL unique categorical values from a column ---
def _extract_categorical_options(series: pd.Series, max_unique: int = 50) -> list:
    """Return sorted unique string values, capped at max_unique."""
    vals = series.dropna().astype(str).unique().tolist()
    vals = sorted(set(vals))
    return vals[:max_unique]


# --- Pre-analyse the full dataframe to extract real filter options ---
def _analyse_filter_columns(df: pd.DataFrame) -> dict:
    """
    Returns a dict keyed by column name with metadata that will be
    injected verbatim into the LLM prompt so the model never has to
    guess which values exist in the data.

    Format:
    {
        "order_date": {
            "is_date": true,
            "years":  ["2023","2024","2025"],
            "months": ["January","February",...,"December"]
        },
        "region": {
            "is_date": false,
            "unique_values": ["East","North","South","West"]
        },
        ...
    }
    """
    analysis = {}
    for col in df.columns:
        series = df[col]
        if _is_date_column(series):
            opts = _extract_date_options(series)
            analysis[col] = {
                "is_date": True,
                "years":   opts["years"],
                "months":  opts["months"],
            }
        else:
            unique_count = series.nunique()
            if unique_count <= 50:
                analysis[col] = {
                    "is_date":      False,
                    "unique_values": _extract_categorical_options(series),
                }
            else:
                analysis[col] = {
                    "is_date":      False,
                    "unique_values": [],  # too many — LLM should skip this as a filter
                    "note":         f"{unique_count} unique values — not suitable as a filter",
                }
    return analysis


# --- LLM Logic to Generate Dashboard JSON ---
def get_dashboard_json_structure():
    """Returns the JSON-template string injected into the LLM prompt.

    Defines the exact shape (header/filters/kpi_cards/charts) the model must
    populate for `generate_dashboard_config()`. Returned as a raw string,
    not parsed — it is embedded verbatim into the prompt sent to OpenAI.
    """
    return """
    {
        "header": {
            "title": "A relevant, concise title for the dashboard based on the data.",
            "subtitle": "A short, descriptive subtitle."
        },
        "filters": [
            {
                "id": "filter1",
                "label": "Label for the first filter",
                "column_name": "exact column name from schema",
                "filter_type": "year",
                "options": ["2023", "2024", "2025"]
            },
            {
                "id": "filter2",
                "label": "Label for second filter",
                "column_name": "exact column name from schema",
                "filter_type": "month",
                "options": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
            }
        ],
        "kpi_cards": [
            {"id": "kpi1", "label": "Label", "calculation": "SUM",          "column_name": "col", "prefix": "$"},
            {"id": "kpi2", "label": "Label", "calculation": "AVERAGE",      "column_name": "col", "prefix": "$"},
            {"id": "kpi3", "label": "Label", "calculation": "COUNT",        "column_name": "col", "prefix": ""},
            {"id": "kpi4", "label": "Label", "calculation": "COUNT_DISTINCT","column_name": "col", "prefix": ""}
        ],
        "charts": [
            { "chart_id": "chart1", "title": "Title", "type": "line",     "data": { "label_column": "col", "value_column": "col", "dataset_label": "Label" }},
            { "chart_id": "chart2", "title": "Title", "type": "doughnut", "data": { "label_column": "col", "value_column": "col", "dataset_label": "Label" }},
            { "chart_id": "chart3", "title": "Title", "type": "bar",      "data": { "label_column": "col", "value_column": "col", "dataset_label": "Label" }},
            { "chart_id": "chart4", "title": "Title", "type": "bar",      "data": { "label_column": "col", "value_column": "col", "dataset_label": "Label" }}
        ]
    }
    """


def generate_dashboard_config(df: pd.DataFrame):
    """
    Use OpenAI (gpt-4o) to analyse a DataFrame and generate a JSON config for
    the dashboard: header, filters (with ground-truth options patched in via
    `_patch_filter_options`), KPI cards, and chart definitions (chart type +
    label/value column pairs).

    Args:
        df: BI query result rows as a DataFrame. NaN values are converted
            to None before analysis.

    Returns
    -------
    tuple[dict | None, int, int, int]
        (dashboard_config, tokens_used, input_tokens, output_tokens)
        dashboard_config is None if the OpenAI call fails or returns
        unparsable JSON. tokens_used is derived from the OpenAI usage object
        when available, falling back to a character-based estimate
        (len // 4) so the caller always receives a non-zero int regardless
        of API behaviour.
    """

    # Clean NaN first
    df = df.where(pd.notnull(df), None)

    # --- PRE-ANALYSE the full dataset for real filter options ---
    column_analysis = _analyse_filter_columns(df)
    column_analysis_str = json.dumps(column_analysis, indent=2)

    data_sample_csv = df.head(30).to_csv(index=False)
    column_info = "\n".join([f"- {col} ({dtype})" for col, dtype in df.dtypes.items()])
    json_structure = get_dashboard_json_structure()

    prompt = f"""
You MUST return ONLY valid JSON wrapped inside ```json ... ```. No extra text. No NaN — use null.

=== SCHEMA ===
{column_info}

=== FULL COLUMN ANALYSIS (derived from the ENTIRE dataset — use these exact values for filter options) ===
{column_analysis_str}

=== DATA SAMPLE (first 30 rows) ===
```csv
{data_sample_csv}
```

=== INSTRUCTIONS ===

1. FILTERS — CRITICAL RULES:
   - For date columns (is_date=true):
       * Create TWO filters using the SAME column_name
       * Filter 1: filter_type="year",  options = the EXACT "years"  list from column_analysis above
       * Filter 2: filter_type="month", options = the EXACT "months" list from column_analysis above
       * NEVER truncate — include ALL years and ALL months present in column_analysis
   - For categorical columns (is_date=false, unique_values non-empty and ≤ 20 items):
       * filter_type="exact", options = the EXACT "unique_values" list
   - Skip columns with too many unique values (note field present) — do NOT use them as filters
   - If no date column exists, pick the two most useful categorical columns

2. KPI CARDS — for each card specify calculation (SUM/AVERAGE/COUNT/COUNT_DISTINCT) and the exact column_name.
   Use prefix="$" for monetary columns.

3. CHARTS — choose meaningful label_column / value_column pairs for the four chart types.

4. OUTPUT — return ONLY the JSON object inside ```json ... ```. Nothing else.

=== JSON STRUCTURE TO POPULATE ===
{json_structure}
"""

    message = ""
    tokens_used   = 0
    input_tokens  = 0
    output_tokens = 0

    try:
        logger.info("Calling LLM (%s) for dashboard config...", DASHBOARD_MODEL)

        result = provider_chat(
            system=(
                "You are a data analyst that returns ONLY valid JSON inside ```json blocks. "
                "Never use NaN — use null. Never truncate filter options."
            ),
            user_prompt=prompt,
            model=DASHBOARD_MODEL,
            temperature=0,
        )

        # --- Capture token usage from the API response ---
        input_tokens  = result.usage.prompt_tokens
        output_tokens = result.usage.completion_tokens
        tokens_used   = result.usage.total_tokens
        usage_reported = tokens_used > 0
        # Fallback: estimate from character lengths if the provider omitted usage
        if not usage_reported:
            tokens_used = max(1, len(prompt) // 4)

        message = result.content.strip()

        # Parse JSON from fenced block
        json_string = message.split("```json")[1].split("```")[0].strip()
        dashboard_config = json.loads(json_string)

        # Add completion side of the estimate to tokens_used if we only had prompt
        if not usage_reported:
            tokens_used += max(1, len(message) // 4)

        # Clean any residual NaN
        dashboard_config = clean_nan(dashboard_config)

        # --- SAFETY PASS: replace any truncated filter options with ground-truth values ---
        dashboard_config = _patch_filter_options(dashboard_config, column_analysis)

        logger.info(f"Dashboard JSON generated and validated successfully (tokens={tokens_used} in={input_tokens} out={output_tokens})")
        return dashboard_config, tokens_used, input_tokens, output_tokens

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}\nRAW RESPONSE:\n{message}")
        tokens_used = max(1, len(prompt) // 4 + len(message) // 4)
        return None, tokens_used, 0, 0
    except Exception as e:
        logger.error(f"ERROR: {e}\nRAW RESPONSE:\n{message}")
        tokens_used = max(1, len(prompt) // 4)
        return None, tokens_used, 0, 0


def _patch_filter_options(config: dict, column_analysis: dict) -> dict:
    """
    Safety net: walk through every filter in the generated config and
    replace its options with the ground-truth values from column_analysis.
    This guarantees the frontend always sees ALL years / months / categories
    even if the LLM accidentally truncated them.
    """
    for f in config.get("filters", []):
        col  = f.get("column_name", "")
        ftype = f.get("filter_type", "exact")

        if col not in column_analysis:
            continue

        meta = column_analysis[col]

        if meta.get("is_date"):
            if ftype == "year":
                f["options"] = meta.get("years", f.get("options", []))
            elif ftype == "month":
                f["options"] = meta.get("months", f.get("options", []))
        else:
            unique_vals = meta.get("unique_values", [])
            if unique_vals:
                f["options"] = unique_vals

    return config