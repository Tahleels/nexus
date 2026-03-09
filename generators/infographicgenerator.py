"""infographicgenerator.py — Nexus AI · Generators

Converts BI query result rows (+ AI-generated insight bullets) into a
structured JSON "infographic" layout: a summary widget, KPI cards, ranked
breakdown lists, and charts. Unlike `dashboard_generator.py` (gpt-4o, one
prompt for the whole dashboard config), this module uses gpt-4o-mini with
JSON-object response mode and pre-computes real statistics (`_analyse_rows`)
so the LLM only has to choose layout/wording, never invent numbers.

Public API:
    InfographicGenerator().generate_infographic_layout(summary_points, rows,
        user=None, agent_name="") -> dict | None

The returned dict (title/subtitle/generated_at/widgets) is returned as JSON
to the front-end infographic layout page for live rendering, and separately
fed into `generators/artifact_builder.py`'s `build_infographic_pdf()` to
produce a downloadable/emailable PDF for scheduled jobs.
"""

import json
import logging
import math
from typing import Optional, Dict, Any, List
from datetime import datetime
from dotenv import load_dotenv
from generator_utils import provider_chat
from llm_providers.models import resolve_default_model

load_dotenv()

from logging_config import get_logger
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Widget builder helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_summary_widget(points: list) -> dict:
    """Builds the "summary_list" widget from AI insight bullet strings.

    Splits each input string on the "•" character so a single string
    containing multiple bullet-separated points is expanded into separate
    list entries.

    Args:
        points: Raw insight strings (each may itself contain multiple
            "•"-separated points).

    Returns:
        `{"widget_type": "summary_list", "title": "AI Insights", "data": [...]}`.
    """
    cleaned = []
    for p in points:
        for part in str(p).split("•"):
            part = part.strip()
            if part:
                cleaned.append(part)
    return {"widget_type": "summary_list", "title": "AI Insights", "data": cleaned}


def _build_kpi_widget(items: list) -> dict:
    """Wraps a list of `{"label", "value"}` dicts into a "kpi_showcase" widget."""
    return {"widget_type": "kpi_showcase", "title": "Key Metrics", "data": items}


def _build_breakdown_widget(title: str, items: list) -> dict:
    """Wraps a list of `{"label", "value"}` dicts into a "breakdown_list" widget."""
    return {"widget_type": "breakdown_list", "title": title, "data": items}


def _build_chart_widget(chart_type: str, title: str, labels=None, datasets=None) -> dict:
    """Wraps labels/datasets into a "chart" widget for the infographic renderer.

    Args:
        chart_type: Chart type string (e.g. "bar", "line", "pie",
            "doughnut", "radar", "bubble"); lower-cased before storage.
        title: Chart title shown in the widget header.
        labels: Optional category labels; omitted from the payload if None.
        datasets: Optional Chart.js-style dataset list; omitted from the
            payload if None.

    Returns:
        `{"widget_type": "chart", "chart_type": ..., "title": ..., "data": {...}}`.
    """
    data_payload: dict = {}
    if labels is not None:
        data_payload["labels"] = labels
    if datasets is not None:
        data_payload["datasets"] = datasets
    return {
        "widget_type": "chart",
        "chart_type": chart_type.lower(),
        "title": title,
        "data": data_payload,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Data analyser — computes real stats BEFORE calling the LLM
#  so the LLM never has to invent numbers
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_rows(rows: list) -> dict:
    """
    Returns a dict:
      columns       : list of column names
      numeric_cols  : { col: {sum, avg, min, max, count, values:[]} }
      text_cols     : { col: {top_values: [(val, count)], unique_count} }
      row_count     : int
    All values are Python-native floats/ints (JSON-safe).
    """
    if not rows:
        return {}

    cols = list(rows[0].keys())
    numeric: Dict[str, Any] = {}
    text: Dict[str, Any]    = {}

    for col in cols:
        vals = [r.get(col) for r in rows if r.get(col) is not None]

        # try numeric
        nums = []
        for v in vals:
            try:
                f = float(str(v).replace(",", ""))
                if not math.isnan(f) and not math.isinf(f):
                    nums.append(f)
            except (ValueError, TypeError):
                pass

        if len(nums) >= len(vals) * 0.6 and nums:          # majority numeric
            numeric[col] = {
                "sum":   round(sum(nums), 4),
                "avg":   round(sum(nums) / len(nums), 4),
                "min":   round(min(nums), 4),
                "max":   round(max(nums), 4),
                "count": len(nums),
                "values": [round(n, 4) for n in nums],      # all values for LLM
            }
        else:
            from collections import Counter
            counts = Counter(str(v) for v in vals if v is not None)
            text[col] = {
                "unique_count": len(counts),
                "top_values":   counts.most_common(10),     # [(val, n), ...]
            }

    return {
        "columns":      cols,
        "numeric_cols": numeric,
        "text_cols":    text,
        "row_count":    len(rows),
    }


def _stats_summary(stats: dict) -> str:
    """Human-readable stats block to inject into the LLM prompt."""
    lines = [f"Total rows: {stats.get('row_count', 0)}"]
    for col, s in stats.get("numeric_cols", {}).items():
        lines.append(
            f"  [{col}] numeric — sum={s['sum']}, avg={s['avg']}, "
            f"min={s['min']}, max={s['max']}, count={s['count']}"
        )
    for col, s in stats.get("text_cols", {}).items():
        top = ", ".join(f'"{v}"({n})' for v, n in s["top_values"][:5])
        lines.append(f"  [{col}] categorical — {s['unique_count']} unique values; top: {top}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Main generator class
# ─────────────────────────────────────────────────────────────────────────────

class InfographicGenerator:
    """
    Converts SQL result rows + AI insights into a structured JSON infographic.
    Uses OpenAI exclusively (gpt-4o-mini fast / gpt-4o for quality).
    """

    MODEL = resolve_default_model("INFOGRAPHIC_MODEL", tier="fast")   # fast + cheap tier for the configured provider

    SYSTEM_PROMPT = """
You are a Data Infographic Planner. Your job is to turn pre-computed statistics
and raw data into a rich, accurate JSON infographic plan.

═══ ABSOLUTE RULES ═══
1. Output ONLY a single valid JSON object. No markdown, no prose, no wrapper keys.
2. Top-level keys MUST be exactly: "title", "subtitle", "widgets"
   — DO NOT nest inside "infographic", "report", "data", or anything else.
3. Every number you use MUST come from the STATISTICS BLOCK or, if none is
   provided, the AI INSIGHTS text. NEVER invent, round up, or estimate values.
4. If a FULL DATASET SAMPLE is provided (non-empty), widgets MUST contain AT
   LEAST 2 "chart" widgets. If no dataset sample is provided (this call is
   based on conversation insights only, no rows of data), do NOT include any
   "chart" widgets at all — there is no numeric series to chart honestly; use
   "kpi_showcase" and "breakdown_list" instead.
5. Use VARIED chart types — do not use "bar" for everything.

═══ WIDGET TYPES ═══

kpi_showcase  — 3-5 headline numbers taken directly from the stats block.
  "data": [{"label": "Total Revenue", "value": "$18,432"}]

breakdown_list — ranked list of categories, max 6 items.
  Top 5 real entries + 1 grouped remainder if >5 items exist.
  "data": [{"label": "Category A", "value": "420 units"}]

chart — visual graph. REQUIRED: widget_type, chart_type, title, data.
  Bar / Line:
    "data": {"labels": ["Jan","Feb"], "datasets": [{"label": "Revenue", "data": [1200, 1800]}]}
  Pie / Doughnut:
    "data": {"labels": ["A","B"], "datasets": [{"label": "Share", "data": [60, 40]}]}
  Radar:
    "data": {"labels": ["Speed","Cost"], "datasets": [{"label": "Product", "data": [80, 60]}]}
  Bubble:
    "data": {"datasets": [{"label": "Group", "data": [{"x":10,"y":20,"r":8}]}]}

═══ CHART SELECTION GUIDE ═══
• Time-series / trends     → "line"
• Category comparisons     → "bar"
• Part-of-whole (≤7 slices)→ "pie" or "doughnut"
• Multi-metric spider web  → "radar"
• Three-variable scatter   → "bubble"
Mix types — one line + one bar + one pie/doughnut is ideal for most datasets.

═══ OUTPUT FORMAT ═══
{
  "title": "...",
  "subtitle": "...",
  "widgets": [ ... ]
}
""".strip()

    # ── public entry point ──────────────────────────────────────────────────

    def generate_infographic_layout(
        self,
        summary_points: List[str],
        rows: List[dict],
        user: Optional[Dict] = None,
        agent_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Parameters
        ----------
        summary_points : AI insight strings from the chat pipeline
        rows           : raw SQL result rows. May be empty — e.g. the
                         "create a document from this conversation" flow has
                         no query results, only `summary_points` from the
                         chat transcript. In that case the infographic is
                         built from insights alone, with no chart widgets
                         (see the conditional chart rule in SYSTEM_PROMPT).
        user           : current_user dict {id, username, role} — used for token tracking.
                         Pass None to skip tracking (e.g. admin or background jobs).
        agent_name     : name of the BI agent that triggered this call

        Returns
        -------
        dict | None
            `{"title", "subtitle", "generated_at", "widgets": [...]}` ready
            to be returned as JSON to the front-end or passed to
            `artifact_builder.build_infographic_pdf()`. Returns None if both
            `rows` and `summary_points` are empty (nothing to build from),
            the LLM call fails, or — only when `rows` is non-empty — no
            chart widget could be assembled even via the `_fallback_chart`
            safety net.
        """
        if not rows and not summary_points:
            logger.warning("InfographicGenerator: no rows or summary_points provided")
            return None

        # Compute real stats first — LLM uses these instead of guessing.
        # Empty for the rows-less (conversation-only) case; _analyse_rows
        # and _stats_summary both degrade gracefully to "Total rows: 0".
        stats = _analyse_rows(rows)
        logger.info(f"InfographicGenerator: analysed {stats.get('row_count', 0)} rows, "
                    f"{len(stats.get('numeric_cols',{}))} numeric cols, "
                    f"{len(stats.get('text_cols',{}))} text cols")

        plan = self._call_llm(summary_points, rows, stats,
                              user=user, agent_name=agent_name)
        if plan is None:
            return None

        return self._assemble(summary_points, plan, stats, rows)

    # ── LLM call ───────────────────────────────────────────────────────────

    def _call_llm(
        self,
        summary_points: List[str],
        rows: List[dict],
        stats: dict,
        user: Optional[Dict] = None,
        agent_name: str = "",
    ) -> Optional[dict]:
        """Calls `self.MODEL` (gpt-4o-mini) to produce a raw infographic plan.

        Sends the pre-computed statistics block, up to 80 sample rows, and
        the AI insight bullets, instructing the model (via `SYSTEM_PROMPT`)
        to choose widget types/chart types and return JSON-object output.
        Also records token usage via `_record_tokens()` on success.

        Args:
            summary_points: AI insight strings from the chat pipeline.
            rows: Raw SQL result rows (only the first 80 are sent to the LLM).
            stats: Pre-computed stats dict from `_analyse_rows(rows)`.
            user: current_user dict for token tracking, or None to skip.
            agent_name: Name of the BI agent that triggered this call.

        Returns:
            The parsed JSON plan dict (not yet unwrapped/assembled), or
            None if the LLM call fails or returns unparsable JSON.
        """

        stats_text   = _stats_summary(stats)
        insight_text = "\n".join(f"• {p}" for p in summary_points if p)

        # Send up to 80 rows as sample; include ALL numeric values for time-series.
        # Empty when this call has no SQL data (conversation-only generation).
        sample_rows = rows[:80]

        if rows:
            data_section = f"""═══ STATISTICS (use ONLY these numbers) ═══
{stats_text}

═══ FULL DATASET SAMPLE (up to 80 rows) ═══
{json.dumps(sample_rows, indent=2, default=str)}"""
            task_requirements = """- kpi_showcase: use exact values from the statistics block above.
- If a date/time column exists and a numeric column exists → include a LINE chart
  using ALL date+value pairs from the dataset sample as chart data points.
- Include at least one BAR or PIE/DOUGHNUT chart in addition to the line chart.
- breakdown_list: use real category values and their real aggregated numbers.
- Do NOT fabricate values. Do NOT round 5000.22 to 5000."""
        else:
            data_section = "═══ FULL DATASET SAMPLE ═══\n(none — this infographic is based on conversation insights only)"
            task_requirements = """- No tabular data was provided — base every widget entirely on the AI INSIGHTS below.
- Do NOT include any "chart" widgets — there is no numeric series to chart honestly.
- Use "kpi_showcase" for any standout numbers explicitly stated in the insights.
- Use "breakdown_list" to group related insight points into a ranked list.
- Do NOT fabricate numbers that aren't present in the insights."""

        user_prompt = f"""
{data_section}

═══ AI INSIGHTS ═══
{insight_text or "No insights provided — derive them from the data."}

═══ YOUR TASK ═══
Generate an infographic JSON plan using the rules in the system prompt.

Specific requirements for THIS dataset:
{task_requirements}

Respond with ONLY the JSON object.
""".strip()

        raw = ""
        try:
            result = provider_chat(
                system=self.SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model=self.MODEL,
                temperature=0.05,           # very low — we want faithful reproduction
                max_tokens=3000,
                response_format={"type": "json_object"},
            )
            raw = result.content or ""
            plan = json.loads(raw)
            logger.info("InfographicGenerator: LLM plan parsed OK")

            # ── TOKEN TRACKING ─────────────────────────────────────────────
            _in_tok  = result.usage.prompt_tokens
            _out_tok = result.usage.completion_tokens
            self._record_tokens(
                user          = user,
                agent_name    = agent_name,
                prompt        = self.SYSTEM_PROMPT + user_prompt,
                response      = raw,
                input_tokens  = _in_tok,
                output_tokens = _out_tok,
            )
            # ──────────────────────────────────────────────────────────────

            return plan
        except json.JSONDecodeError as e:
            logger.error(f"InfographicGenerator: JSON parse error — {e}")
            logger.error(f"Raw: {raw[:600]}")
            return None
        except Exception as e:
            logger.error(f"InfographicGenerator: LLM call failed — {e}")
            return None

    # ── Unwrap nested LLM responses ────────────────────────────────────────

    def _unwrap(self, plan: dict) -> dict:
        """Finds the `{"widgets": [...]}` dict, even if the LLM nested it under an extra key.

        Despite `SYSTEM_PROMPT` instructing the model to return top-level
        `title`/`subtitle`/`widgets` keys, models sometimes wrap the plan
        under a key like "infographic" or "data" anyway — this digs it out.

        Args:
            plan: Raw parsed JSON dict returned by `_call_llm()`.

        Returns:
            The dict containing a `"widgets"` key — either `plan` itself,
            a known nested key's value, or the first dict value found that
            has a `"widgets"` key. Falls back to returning `plan` unchanged
            (with a logged warning) if no such dict is found.
        """
        if "widgets" in plan:
            return plan
        for key in ("infographic", "report", "data", "result", "output", "layout"):
            if key in plan and isinstance(plan[key], dict) and "widgets" in plan[key]:
                logger.info(f"InfographicGenerator: unwrapped nested key '{key}'")
                return plan[key]
        for key, val in plan.items():
            if isinstance(val, dict) and "widgets" in val:
                logger.info(f"InfographicGenerator: unwrapped nested key '{key}'")
                return val
        logger.warning(f"InfographicGenerator: no 'widgets' found; keys={list(plan.keys())}")
        logger.warning(f"InfographicGenerator: plan dump → {json.dumps(plan, default=str)[:800]}")
        return plan

    # ── Assemble final payload ─────────────────────────────────────────────

    def _assemble(
        self,
        summary_points: List[str],
        plan: dict,
        stats: dict,
        rows: List[dict],
    ) -> Optional[Dict[str, Any]]:
        """Turns the raw LLM plan into the final infographic payload.

        Unwraps the plan (`_unwrap`), guarantees at least one chart widget
        is present (injecting `_fallback_chart()` if the LLM omitted one),
        prepends an AI-insights summary widget, then rebuilds every widget
        through the local `_build_*_widget()` helpers (re-validating shape
        and normalizing field names along the way).

        Args:
            summary_points: AI insight strings to render as the summary widget.
            plan: Raw JSON plan dict from `_call_llm()`.
            stats: Pre-computed stats dict from `_analyse_rows()`, used only
                if a fallback chart needs to be synthesized.
            rows: Raw SQL result rows, used only by the fallback chart.

        Returns:
            The final `{"title", "subtitle", "generated_at", "widgets"}`
            dict, or None if no chart widget exists and the fallback chart
            could not be built either.
        """

        plan = self._unwrap(plan)
        widgets_plan = plan.get("widgets", [])

        # Log what came back
        types_found = [w.get("widget_type", "?") for w in widgets_plan]
        logger.info(f"InfographicGenerator: widget types → {types_found}")

        # Inject fallback chart if none present — only when there's actual row
        # data to chart. A conversation-only call (rows=[]) has no numeric
        # series to fall back to, and SYSTEM_PROMPT already tells the model
        # not to include charts in that case, so requiring one here would
        # make every rows-less generation fail.
        has_chart = any(w.get("widget_type") == "chart" for w in widgets_plan)
        if not has_chart and rows:
            logger.warning("InfographicGenerator: no chart in plan — building fallback")
            fb = self._fallback_chart(stats, rows)
            if fb:
                widgets_plan = widgets_plan + [fb]
            else:
                logger.error("InfographicGenerator: fallback chart failed — aborting")
                return None

        final: Dict[str, Any] = {
            "title":        plan.get("title", "Data Report"),
            "subtitle":     plan.get("subtitle", "Nexus AI · Auto-generated overview"),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "widgets":      [],
        }

        # Prepend AI insights widget
        if summary_points:
            final["widgets"].append(_build_summary_widget(summary_points))

        for wp in widgets_plan:
            wtype = wp.get("widget_type", "")
            try:
                if wtype == "kpi_showcase":
                    final["widgets"].append(_build_kpi_widget(wp["data"]))

                elif wtype == "breakdown_list":
                    final["widgets"].append(
                        _build_breakdown_widget(
                            title=wp.get("title", "Breakdown"),
                            items=wp["data"],
                        )
                    )

                elif wtype == "chart":
                    chart_data = wp.get("data", {})
                    # chart_data may be a dict {labels, datasets} or a list
                    final["widgets"].append(_build_chart_widget(
                        chart_type=wp.get("chart_type", "bar"),
                        title=wp.get("title", "Chart"),
                        labels=chart_data.get("labels") if isinstance(chart_data, dict) else None,
                        datasets=chart_data.get("datasets") if isinstance(chart_data, dict) else chart_data,
                    ))

                else:
                    logger.warning(f"InfographicGenerator: unknown widget type '{wtype}'")

            except (KeyError, TypeError) as e:
                logger.error(f"InfographicGenerator: error building '{wtype}' — {e}")
                continue

        if not final["widgets"]:
            logger.error("InfographicGenerator: no widgets could be assembled — aborting")
            return None

        return final

    # ── Fallback chart from raw stats ──────────────────────────────────────

    def _fallback_chart(self, stats: dict, rows: List[dict]) -> Optional[dict]:
        """Builds a chart purely from analysed stats when the LLM omits one.

        Chart-type selection heuristic, tried in order:
          1. Time-series: if a column name matches date/time/month/year/day/
             period AND at least one numeric column exists -> "line" chart
             of that numeric column over the date column (first 50 pairs).
          2. Categorical + numeric: if any text column and a numeric column
             exist -> "bar" chart of the numeric column summed per the top
             10 most common values of the first text column.
          3. Pure numeric: if 2+ numeric columns exist -> "doughnut" chart
             of each numeric column's sum (first 6 columns).

        Args:
            stats: Pre-computed stats dict from `_analyse_rows()`.
            rows: Raw SQL result rows used to build the data series.

        Returns:
            A chart widget dict from `_build_chart_widget()`, or None if
            none of the three cases apply (e.g. no numeric columns at all).
        """
        num_cols  = stats.get("numeric_cols", {})
        text_cols = stats.get("text_cols",    {})

        # Case 1: time-series (date col + numeric col)
        cols = stats.get("columns", [])
        date_col = next((c for c in cols if any(k in c.lower() for k in ("date","time","month","year","day","period"))), None)
        num_col  = next(iter(num_cols), None)
        if date_col and num_col and rows:
            pairs = [(str(r.get(date_col,"")), r.get(num_col)) for r in rows if r.get(num_col) is not None]
            pairs = [(d, float(v)) for d, v in pairs if v is not None][:50]
            if pairs:
                labels, values = zip(*pairs)
                return _build_chart_widget("line", f"{num_col} over time",
                                           labels=list(labels),
                                           datasets=[{"label": num_col, "data": list(values)}])

        # Case 2: categorical + numeric → bar chart
        if text_cols and num_col:
            text_col = next(iter(text_cols))
            tc = text_cols[text_col]
            labels = [v for v, _ in tc["top_values"][:10]]
            # aggregate numeric per category
            agg: Dict[str, float] = {}
            for row in rows:
                k = str(row.get(text_col, ""))
                v = row.get(num_col)
                if k in labels and v is not None:
                    try: agg[k] = agg.get(k, 0) + float(v)
                    except: pass
            values = [round(agg.get(l, 0), 2) for l in labels]
            return _build_chart_widget("bar", f"{num_col} by {text_col}",
                                       labels=labels,
                                       datasets=[{"label": num_col, "data": values}])

        # Case 3: pure numeric columns → doughnut of their sums
        if len(num_cols) >= 2:
            labels = list(num_cols.keys())[:6]
            values = [round(num_cols[c]["sum"], 2) for c in labels]
            return _build_chart_widget("doughnut", "Column Totals",
                                       labels=labels,
                                       datasets=[{"label": "Total", "data": values}])

        return None

    # ── Token tracking ────────────────────────────────────────────────────

    def _record_tokens(
        self,
        user: Optional[Dict],
        agent_name: str,
        prompt: str,
        response: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Records OpenAI token usage for this generation against the requesting user.

        No-op if `user` is None/falsy or has no "id" (e.g. background jobs
        running without a real user context). Falls back to
        `token_limits.estimate_tokens()` if explicit input/output token
        counts are not available.

        Args:
            user: current_user dict (must contain "id") to attribute usage to.
            agent_name: Name of the BI agent that triggered this generation.
            prompt: Full prompt text sent to the LLM (used for estimation fallback).
            response: Raw LLM response text (used for estimation fallback).
            input_tokens: Prompt token count from the OpenAI usage object, if known.
            output_tokens: Completion token count from the OpenAI usage object, if known.
        """
        if not user or not user.get("id"):
            return

        try:
            import token_limits as _tl
            _in  = int(input_tokens  or 0)
            _out = int(output_tokens or 0)
            tokens = _in + _out or _tl.estimate_tokens(prompt, response)
            _tl.record_usage(
                user_id       = user["id"],
                tokens        = tokens,
                call_type     = "infographic",
                agent_name    = agent_name or "",
                question      = f"infographic generation ({agent_name})",
                input_tokens  = _in,
                output_tokens = _out,
                model         = self.MODEL,
            )
            logger.info(
                f"[token] user={user.get('username', user['id'])} "
                f"role={user.get('role', 'user')} type=infographic tokens={tokens} agent={agent_name or '-'}"
            )
        except Exception as e:
            logger.warning(f"InfographicGenerator: token recording failed — {e}")

