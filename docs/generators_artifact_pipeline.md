# Generators / Artifact Pipeline — Deep Dive

How a BI query result (rows + columns from the NLQ/BI engine) becomes a rendered artifact — dashboard, report, infographic, or PowerPoint — and how that artifact gets back to the user (live JSON render in the browser, or a PDF/PPTX email attachment from a scheduled job).

Verified directly against code on 2026-06-29. Every file/line reference below was read, not inferred — if you change these files, this doc will drift, so re-verify before trusting it months from now.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Two Consumption Paths: Interactive vs Scheduled](#two-consumption-paths)
3. [Dashboard — `dashboard_generator.py`](#dashboard-generator)
4. [Report — `reportgenerator.py`](#report-generator)
5. [Infographic — `infographicgenerator.py`](#infographic-generator)
6. [Chart-Type Selection Heuristics](#chart-type-selection-heuristics)
7. [`chartselector.py` — Unused Widget Builders](#chartselector)
8. [Presentation (PPT) — `ppt_generator.py`](#ppt-generator)
9. [Artifact Builder — `artifact_builder.py`](#artifact-builder)
10. [Return Shapes & Persistence Summary](#return-shapes--persistence-summary)
11. [Configuration Reference](#configuration-reference)
12. [Key Libraries](#key-libraries)
13. [Notable Findings](#notable-findings)

---

## Architecture Overview

```
                 NLQ / BI engine (nlq_engine.py) — outside this doc's scope
                                   │
                                   ▼
                 rows: List[Dict]  +  columns: List[str]  (+ insights)
                                   │
        ┌──────────────────────────┼───────────────────────────┬────────────────────┐
        ▼                          ▼                           ▼                    ▼
 dashboard_generator.py     reportgenerator.py         infographicgenerator.py   ppt_generator.py
 generate_dashboard_config  generate_report_config     InfographicGenerator      PresentationGenerator
 (OpenAI gpt-4o)            (OpenAI gpt-4o-mini,        .generate_infographic_    .generate() /
                            rule-based fallback)        layout()                  .optimize()
                            get_report_data_with_                                 (OpenAI gpt-4o,
                            filters() — re-filter                                  + gpt-4.1-mini web
                            without re-calling LLM                                 search for slide 3)
        │                          │                           │                    │
        ▼                          ▼                           ▼                    ▼
  config: dict              report_config: dict          layout: dict          result: dict
  (header/filters/          (header/filters/kpis/        (title/subtitle/      (status/slides/
   kpi_cards/charts)         tables/insights)             generated_at/         client/pptx_base64)
                                                           widgets[])
        │                          │                           │                    │
        └──────────────┬───────────┴─────────────┬─────────────┘                    │
                        ▼                         ▼                                  │
              ════════ INTERACTIVE PATH (app.py) ════════                            │
              Routes return the config/layout dict as JSON.                         │
              Front-end (dashboardlayout.html / reportlayout.html /                 │
              infographiclayout.html / presentationlayout.html) renders             │
              it live with Chart.js — no PDF/PPTX involved.                         │
                                                                                       │
              ════════ SCHEDULED-JOB PATH (services/scheduler_service.py) ════════   │
              Same config dicts are fed into:                                       │
                  artifact_builder.build_dashboard_pdf(config, raw_data)            │
                  artifact_builder.build_report_pdf(report_config, raw_data)        │
                  artifact_builder.build_infographic_pdf(infographic)               │
              ppt_generator's PresentationGenerator.generate() already returns ◄────┘
              a base64 PPTX (`pptx_base64`) — no extra artifact_builder step.
                        │
                        ▼
              PDF/PPTX bytes attached to an outbound email via
              scheduler_service._send_email() — NOT written to disk,
              NOT returned to a browser. Only {tool, filename} metadata
              is persisted via execution_logger.finish_execution().
```

---

## Two Consumption Paths

There are exactly two ways these generators' output reaches a user, and the path determines what is returned.

| Path | Driver | What runs | What's returned/persisted |
|---|---|---|---|
| **Interactive (live render)** | `app.py` HTTP routes (`/api/bi-agents/generate-dashboard`, `-report`, `-infographic`, `-presentation`) | Calls the generator module directly, gets back a config/layout **dict** (or PPTX base64 for presentations) | `jsonify(...)` / `Response(json.dumps(...))` straight to the browser; front-end renders charts client-side (Chart.js). Also persisted as BI training data via `_bi_td.save_async(...)`. |
| **Scheduled (email delivery)** | `services/scheduler_service.py` `_execute_job()` (around line 286) | Calls the same generator functions, then additionally calls `generators/artifact_builder.py` to turn the config into **PDF bytes** (or uses the PPTX bytes ppt_generator already produced) | Bytes are attached to an email via `_send_email()` (scheduler_service.py:231); **never written to disk**. `execution_logger.finish_execution()` records only `{tool, filename}` metadata, not the bytes themselves. |

Confirmed by reading `app.py`: `build_dashboard_pdf` / `build_report_pdf` / `build_infographic_pdf` are imported and used **only** inside `services/scheduler_service.py` (lines 38-42, 372, 396, 431) — `app.py` never calls `artifact_builder` directly. The interactive routes return raw JSON.

---

## Dashboard Generator

**File:** `generators/dashboard_generator.py`

`generate_dashboard_config(df: pd.DataFrame)` (line 192) is the entry point. Flow:

1. `_analyse_filter_columns(df)` (line 100) walks every column and classifies it as a date column (via `_is_date_column`, line 58, and `_extract_date_options`, line 76, which pulls every distinct year/month — not just a sample) or a categorical column (via `_extract_categorical_options`, line 92, capped at 50 uniques). Columns with >50 uniques are flagged with a `note` so the LLM skips them as filters.
2. `get_dashboard_json_structure()` (line 147) returns the literal JSON-template string (header/filters/kpi_cards/charts) embedded in the prompt.
3. A single OpenAI **gpt-4o** call (temperature 0) is made with the schema, the full `column_analysis`, and a 30-row CSV sample. The prompt explicitly tells the model which chart types to use for which of the 4 chart slots (line/doughnut/bar/bar — see [Chart-Type Selection Heuristics](#chart-type-selection-heuristics)).
4. The fenced ```json block is parsed; NaNs are scrubbed via `clean_nan()` (line 39).
5. `_patch_filter_options()` (line 327) is a **safety net** — it overwrites any filter's `options` list with the ground-truth values from `column_analysis`, in case the LLM truncated a long list of years/months/categories.

**Returns:** `(dashboard_config: dict | None, tokens_used: int, input_tokens: int, output_tokens: int)`. `dashboard_config` is `None` on any OpenAI/JSON failure.

**Consumers:**
- `app.py` line 1429 (`/api/bi-agents/generate-dashboard`) — wraps it as `{"type": "dashboardData", "config": ..., "rawData": ...}` and returns it as a raw `Response(json.dumps(...))` (not `jsonify`, to allow `allow_nan=False` strictness) for the front-end to render live.
- `services/scheduler_service.py` line 361 (`_get_dashboard_gen()`) — same config dict, but passed into `artifact_builder.build_dashboard_pdf()` for the "dashboard" scheduled-job tool.

---

## Report Generator

**File:** `generators/reportgenerator.py` (UTF-8 BOM at the start of the file — left untouched)

`generate_report_config(df: pd.DataFrame) -> tuple` (line 309) is the entry point. Flow:

1. `_column_meta(df)` (line 91) classifies every column as `"numeric"` / `"date"` / `"categorical"` via `_is_numeric()` (line 63) / `_is_date()` (line 68 — datetime dtype, or ≥70% of a 30-row string sample matching a loose date-like regex).
2. `_build_fallback_config(df, meta)` (line 123) builds a **complete, zero-token, rule-based** report config (filters from low-cardinality categoricals, a single data table with SUM/GROUP aggregations per column kind, up to 4 KPI cards) — this is what ships if OpenAI is unreachable.
3. `_llm_config(df, meta, fallback)` (line 203) sends the fallback config to **gpt-4o-mini** as a "starter config to improve", asking it to retitle/reformat columns, choose up to 3 filters and 4 KPIs, and write 2-3 plain-English insights. On any failure (`OpenAI` unset, network error, bad JSON) it returns the untouched `fallback` with `tokens_used=0`.
4. `_safe_json()` (line 47) scrubs NaN/Inf before the config is returned.

**Returns:** `(config: dict, tokens_used: int, input_tokens: int, output_tokens: int)`. Never `None` — always at least the rule-based fallback.

A second public function, `get_report_data_with_filters(df, filters=None)` (line 337), re-applies simple equality filters to the **same** DataFrame and returns `{"columns": [...], "rows": [...]}` — used so the front-end can re-filter a report's table **without** another LLM call.

**Consumers:**
- `app.py` line 1595 (`/api/bi-agents/generate-report`) — returns `{"success": True, "report_config": ..., "raw_data": ...}` as JSON for `reportlayout.html` to render.
- `services/scheduler_service.py` line 385 (`_get_report_gen()`) — same config dict fed into `artifact_builder.build_report_pdf()`.

---

## Infographic Generator

**File:** `generators/infographicgenerator.py`

`InfographicGenerator` (class, line 176) is the entry point via `generate_infographic_layout(summary_points, rows, user=None, agent_name="")` (line 245). Flow:

1. `_analyse_rows(rows)` (line 104) pre-computes **real** statistics per column — `numeric_cols` (sum/avg/min/max/count + every value) or `text_cols` (unique count + top-10 most common values via `collections.Counter`) — so the LLM is never asked to invent a number.
2. `_call_llm()` (line 289) sends `_stats_summary(stats)` (line 158, a human-readable text block) plus up to 80 sample rows to **gpt-4o-mini** with `response_format={"type": "json_object"}`, temperature 0.05. `SYSTEM_PROMPT` (class attribute, line 184) is the authoritative chart-selection guide (see next section) and mandates ≥2 chart widgets with varied types.
3. `_unwrap()` (line 387) defensively digs the `{"widgets": [...]}` payload out of common nesting mistakes (`{"infographic": {...}}`, `{"report": {...}}`, etc.) in case the model ignores the "no wrapper keys" instruction.
4. `_assemble()` (line 419) guarantees at least one chart widget exists — injecting `_fallback_chart(stats, rows)` (line 511) if the LLM's plan has none — then rebuilds every widget through the module's private `_build_summary_widget` (line 39) / `_build_kpi_widget` (line 62) / `_build_breakdown_widget` (line 67) / `_build_chart_widget` (line 72) helpers and prepends an AI-insights summary widget from `summary_points`.
5. `_record_tokens()` (line 578) attributes OpenAI usage to `user["id"]` via `token_limits.record_usage()`, skipped entirely if `user` is falsy.

**Returns:** `Optional[Dict]` — `{"title", "subtitle", "generated_at", "widgets": [...]}`, or `None` if `rows` is empty, the LLM call fails, or no chart could be assembled even via the fallback.

**Consumers:**
- `app.py` line 1659 (`/api/bi-agents/generate-infographic`) — returns `{"status": "success", "infographic": layout, "token_usage": ...}` as JSON for `infographiclayout.html`.
- `services/scheduler_service.py` line 413 (`_get_infographic_gen()`) — same `layout` dict fed into `artifact_builder.build_infographic_pdf()`.

---

## Chart-Type Selection Heuristics

There is **no shared/centralized** chart-type selector across the codebase — each generator decides chart types independently, at a different point in its pipeline:

| Generator | Who decides the chart type | Mechanism |
|---|---|---|
| `dashboard_generator.py` | The LLM (gpt-4o) | The prompt's `json_structure` (from `get_dashboard_json_structure()`, line 147) hard-codes 4 chart slots as `"line"`, `"doughnut"`, `"bar"`, `"bar"` — the model only chooses *which columns* fill each slot, not the type itself. |
| `infographicgenerator.py` | The LLM (gpt-4o-mini) | `InfographicGenerator.SYSTEM_PROMPT`'s "CHART SELECTION GUIDE" section (lines ~216-222): time-series→line, category comparison→bar, part-of-whole (≤7 slices)→pie/doughnut, multi-metric→radar, 3-variable→bubble. The model picks freely; `_assemble()` only enforces "≥1 chart widget exists", falling back to `_fallback_chart()` (line 511) if not, which applies its own 3-case heuristic (time-series column detected by name match → line; categorical+numeric → bar; ≥2 numeric columns → doughnut of sums). |
| `ppt_generator.py` | N/A | PPT slides contain text bullets/columns, not charts — no chart-type decision exists in this module. |
| `artifact_builder.py` | Whoever built the upstream `config`/`infographic` dict | `_build_chart_drawing()` (line 290) is a pure **dispatch** function: `"bar"`→`_bar_chart()` (line 176), `"doughnut"`/`"pie"`→`_pie_chart()` (line 215), `"line"`→`_line_chart()` (line 254), anything else (e.g. `"bubble"`, `"radar"`, an unrecognized string) **silently falls back to a bar chart** — reportlab has no native bubble/radar chart used here, so those two chart types from the infographic LLM prompt cannot actually be rendered in the PDF path; they would only render correctly in the Chart.js-based interactive front-end. |
| `chartselector.py` | N/A (dead code) | See next section. |

---

## `chartselector.py`

**File:** `generators/chartselector.py`

Defines four public `build_*_widget()` helpers (`build_kpi_widget` line 25, `build_breakdown_widget` line 34, `build_chart_widget` line 43, `build_summary_widget` line 175) that produce the same JSON widget shapes as `infographicgenerator.py`'s private `_build_*_widget()` functions, plus `GEMINI_COLORS` (line 23), a 7-color palette.

`build_chart_widget()` is the most substantial: it drops any label (and the corresponding value in every dataset) that is `None`/blank, drops datasets left empty after that, then applies Chart.js styling per chart type (line/radar get a translucent fill + 2px border; bubble gets a semi-transparent fill; bar/pie/doughnut get the `GEMINI_COLORS` palette), and wraps everything in a full Chart.js `{type, data, options}` config (with `cutout: "60%"` for doughnut, empty `scales` for pie/doughnut/radar).

**This module has no internal callers** — confirmed via repo-wide search: nothing imports `chartselector` except a logging-category registration in `core/logging_config.py` (line 71, the `"generators"` log group lists `app.chartselector` alongside the other 5 generator modules, purely for log routing). `infographicgenerator.py` defines its own equivalent `_build_*_widget()` functions rather than importing from here. It is effectively dead code / a superseded earlier version of the same widget-building logic — kept in the codebase but not wired into the pipeline.

---

## PPT Generator

**File:** `generators/ppt_generator.py`

`PresentationGenerator` (class, line 529) has two public entry points:

### `generate()` — line 557

1. **Data quality gate**: `_validate_data_for_ppt(rows, question)` (line 402) hard-rejects 0 rows, and soft-rejects too-few/too-numeric datasets (returns `{"status": "insufficient_data", ...}` without calling the LLM at all).
2. **Client resolution**: `detect_client()` (line 100) tries, in order: exact/fuzzy match of a known client name in the question text, then in row `MeetingTitle` fields, then `_infer_name_from_titles()` (line 183, looks for a recurring first-word across meeting titles), then `_infer_name_from_question()` (line 229, quoted text / "for X" trigger phrases / capitalized proper nouns), finally an `"unknown_client"` shell. Detected/inferred clients get `"_dynamic": True`.
3. **Context assembly**: `_summarise_meeting_rows()` (line 281, full notes for the 5 most recent meetings, 150-char-truncated for the next 9), `_format_history_context()` (line 332, last 3 prior PPTs for this client from `clients_db`), and `_fetch_competitor_intelligence()` (line 474, a **live web search** via `gpt-4.1-mini`'s `responses.create(tools=[{"type": "web_search_preview"}])`, capped to 3000 chars, used only for slide 3).
4. **LLM call**: `_call_openai()` (line 883) sends one big prompt to `self.model` (env `OPENAI_PPT_MODEL`, default **gpt-4o**) asking for exactly 3 slide JSON objects (themes: `"delivery"`, `"process"`, `"competitive"`).
5. **Schema validation**: `_validate_slides()` (line 352) enforces exactly 3 slides, each with exactly 3 columns, each column padded/truncated to exactly 3 bullets, and a `next_action` field (migrated from the old `bottom_message` key if present). Raises `ValueError` on unrecoverable shape mismatches.
6. **History persistence**: `_store_ppt_history()` (line 73) saves the slides to `clients_db` — **skipped** for dynamically-inferred clients (`is_dynamic=True`).
7. **PPTX render**: `_build_pptx()` (line 1028) — see below. Failures here are caught and logged; `generate()` still returns successfully with `pptx_base64: None`.

**Returns:** `{"status": "success", "slides": [...], "client": {...}, "client_id": str, "pptx_base64": str | None, "generated_at": str, "tokens_used": int, "email_enriched": bool, "data_summary": {...}}`, or `{"status": "insufficient_data", "message": str, "row_count": int}`.

### `optimize()` — line 679

Takes existing slides + a free-text instruction, validates the instruction is actually a slide-editing request via `_validate_optimize_instruction()` (line 833, its own small LLM call classifying `valid`/`irrelevant`/`out_of_scope`), then asks `self.model` to return updated slides + a one-line change summary, re-validates via `_validate_slides()`, and re-renders the PPTX. Returns `{"status": "invalid_instruction", "message": ...}` if the instruction is rejected.

### `_build_pptx()` — line 1028

Pure `python-pptx` rendering, **not** reportlab (unlike the other three artifact types, which all go through `artifact_builder.py`). Builds a 13.333"×7.5" (16:9) deck: one slide per slide-dict, themed by `THEMES["delivery"|"process"|"competitive"]` (local dict, line 1058), using local closures `rect()`, `oval()`, `txt()`, `txt_multi()` to draw rounded-rectangle cards, numbered ovals, and word-wrapped text boxes. Every text run is truncated via `_truncate_bullet()` (line 450, default 140 chars, 200 for titles) to prevent PPTX shape overflow.

**Returns:** the PPTX file **base64-encoded as a string** directly (not raw bytes like the PDF builders) — `_build_pptx()` returns `base64.b64encode(buf.read()).decode("utf-8")` itself; there is no separate `artifact_builder` step for presentations.

**Consumers:**
- `app.py` line 2015 (`/api/bi-agents/generate-presentation`) and line 2092 (`/api/bi-agents/optimize-presentation`) — return the full result dict (including `pptx_base64`) as JSON; the front-end presumably offers it as a download.
- `services/scheduler_service.py` line 485 (`_get_ppt_gen()`) — calls `.generate()` for the `"ppt"` scheduled-job tool, then attaches `ppt_result["pptx_base64"]` (with `is_base64: True`) directly to the email — **no `artifact_builder` call for PPT**, since the base64 PPTX is already final output.

---

## Artifact Builder

**File:** `generators/artifact_builder.py`

The orchestrating entry point for **PDF** artifacts only (dashboard/report/infographic — not PPT, which is self-contained in `ppt_generator.py`). Built entirely on **reportlab**, no browser/headless-Chrome dependency. Public API (module docstring, lines 1-11):

```python
build_dashboard_pdf(config, raw_data)     -> bytes
build_report_pdf(report_config, raw_data) -> bytes
build_infographic_pdf(infographic)        -> bytes
```

Shared internals:
- `_clean_nan()` (line 63) / `_fmt_num()` (line 78, abbreviates large numbers to K/M, or formats as currency with a `"$"` prefix).
- `_base_styles()` (line 106) — one shared `ParagraphStyle` sheet (brand colors, KPI value/label styles, table header/cell styles) used by all three builders.
- `_header_flowable()` (line 152) — the dark gradient-style title banner common to all three PDF types.
- `_bar_chart()` / `_pie_chart()` / `_line_chart()` (lines 176/215/254) — reportlab `Drawing` builders, one series each.
- `_build_chart_drawing()` (line 290) — the type-dispatch function described in [Chart-Type Selection Heuristics](#chart-type-selection-heuristics); unrecognized types silently render as a bar chart.
- `_kpi_table()` (line 329) — up to 4 KPI cards rendered as a 2-row table (value row + label row).

### `build_dashboard_pdf(config, raw_data)` — line 378

Landscape A4. Header banner → KPI strip (aggregates `raw_data` per `config["kpi_cards"]`'s `calculation`/`column_name`, supporting SUM/AVERAGE/COUNT/COUNT_DISTINCT) → charts from `config["charts"]` (aggregated by `label_column`/`value_column`, sorted descending and capped to top 8 for bar/doughnut, sorted by label for line, rendered 2-per-row).

### `build_report_pdf(report_config, raw_data)` — line 518

Landscape A4. Header banner → a single data table covering every column of the first `raw_data` row (column width clamped 18mm-55mm, evenly divided), capped to the **first 500 rows** with a footnote if more exist. Note: only `report_config["header"]` is actually used here — the richer `filters`/`kpis`/`tables` structure produced by `reportgenerator.py` is not rendered into this PDF (see [Notable Findings](#notable-findings)).

### `build_infographic_pdf(infographic)` — line 632

Portrait A4. Header banner (purple accent) → iterates `infographic["widgets"]` by `widget_type`/`type`: `"summary"` → bulleted key-insights list, `"kpi_showcase"` → KPI strip via `_kpi_table()`, `"breakdown_list"` → a 2-column ranked table, `"chart"` → accumulated chart Drawings flushed 2-per-row at the end.

All three functions return raw PDF **bytes** (`buf.getvalue()` from an `io.BytesIO()` `SimpleDocTemplate` target) — never written to a file path, never base64-encoded.

---

## Return Shapes & Persistence Summary

| Generator | Function | Return shape | Where it's used |
|---|---|---|---|
| `dashboard_generator.py` | `generate_dashboard_config(df)` | `(dict \| None, int, int, int)` | JSON to browser (`app.py:1429`); fed to `build_dashboard_pdf` (`scheduler_service.py:361`) |
| `reportgenerator.py` | `generate_report_config(df)` | `(dict, int, int, int)` | JSON to browser (`app.py:1595`); fed to `build_report_pdf` (`scheduler_service.py:385`) |
| `reportgenerator.py` | `get_report_data_with_filters(df, filters)` | `{"columns": [...], "rows": [...]}` | Re-filtering an already-generated report client-side, no LLM call |
| `infographicgenerator.py` | `InfographicGenerator.generate_infographic_layout(...)` | `dict \| None` | JSON to browser (`app.py:1659`); fed to `build_infographic_pdf` (`scheduler_service.py:413`) |
| `ppt_generator.py` | `PresentationGenerator.generate(...)` / `.optimize(...)` | `dict` incl. `pptx_base64: str \| None` | JSON to browser incl. base64 PPTX (`app.py:2015`/`2092`); base64 string attached directly to email (`scheduler_service.py:485`) — **bypasses `artifact_builder.py` entirely** |
| `artifact_builder.py` | `build_dashboard_pdf` / `build_report_pdf` / `build_infographic_pdf` | `bytes` (raw PDF) | Email attachments only, via `scheduler_service._send_email()` (line 231) — never served over HTTP, never written to disk |

**Nothing in this pipeline writes to disk.** Every artifact either stays in memory as a dict/JSON response, or is attached to an outbound email as bytes/base64 and discarded after `smtplib.SMTP.sendmail()` returns. `execution_logger.finish_execution()` (called from `scheduler_service.py` after job completion) persists only `{"tool": a["tool"], "filename": a["filename"]}` per artifact — the actual PDF/PPTX bytes are not retained anywhere once the email is sent.

---

## Configuration Reference

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | — | Required by `dashboard_generator.py` (raises at import time if missing), `infographicgenerator.py`'s `InfographicGenerator.__init__`, and `ppt_generator.py`'s `PresentationGenerator.__init__`. `reportgenerator.py` degrades gracefully (logs a warning, uses the rule-based fallback) if missing. |
| `OPENAI_PPT_MODEL` | `gpt-4o` | Model used by `PresentationGenerator._call_openai()` and `.optimize()` for slide generation. |
| `OPENAI_SEARCH_MODEL` | `gpt-4.1-mini` | Model used by `_fetch_competitor_intelligence()` for the live web-search call powering PPT slide 3. |
| (hard-coded) `dashboard_generator._MODEL`-equivalent | `gpt-4o` | `generate_dashboard_config()` calls `client.chat.completions.create(model="gpt-4o", ...)` directly — not env-configurable. |
| `reportgenerator._MODEL` | `gpt-4o-mini` | Hard-coded module constant (line 40). |
| `InfographicGenerator.MODEL` | `gpt-4o-mini` | Class attribute (line 182); comment notes it can be swapped to `gpt-4o` for higher quality. |
| SMTP env vars (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`) | `smtp.gmail.com` / `587` / — | Read by `scheduler_service._send_email()` — if `SMTP_USER`/`SMTP_PASS` are unset, email delivery (and therefore the only consumer of `artifact_builder.py`'s output) is silently skipped. |

---

## Key Libraries

| Library | Used for |
|---|---|
| `reportlab` | All PDF rendering in `artifact_builder.py` (Platypus flowables + graphics charts) |
| `python-pptx` | PPTX rendering in `ppt_generator.py._build_pptx()` |
| `openai` | All four LLM-backed generators (`dashboard_generator`, `reportgenerator`, `infographicgenerator`, `ppt_generator`) |
| `pandas` | DataFrame analysis in `dashboard_generator.py` and `reportgenerator.py` |
| `python-dotenv` | `.env` loading (`load_dotenv()`) in every generator module |

---

## Notable Findings

- **`chartselector.py` is dead code.** Its four `build_*_widget()` exports have no internal callers anywhere in the repo; `infographicgenerator.py` independently re-implements equivalent `_build_*_widget()` private helpers instead of importing from this module. Only referenced by `core/logging_config.py` for log-category routing.
- **Bubble/radar charts cannot render in the PDF path.** `infographicgenerator.py`'s LLM prompt explicitly allows `"radar"` and `"bubble"` chart types, but `artifact_builder._build_chart_drawing()` only implements bar/pie/line — any other type (including radar/bubble) silently falls back to a bar chart when rendered into a PDF via `build_infographic_pdf()`. The interactive (Chart.js) front-end path presumably supports them; the scheduled-PDF path does not.
- **`build_report_pdf()` under-uses `reportgenerator.py`'s richer config.** `reportgenerator.generate_report_config()` produces `filters`, `kpis`, `tables[].columns` with per-column `format`/`aggregation`, and `insights` — but `artifact_builder.build_report_pdf()` only reads `report_config["header"]` and otherwise renders a flat, unaggregated dump of `raw_data` (all columns, no grouping, no KPIs, no insights). The KPI cards and insights generated by the LLM are visible in the interactive JSON path but never appear in the scheduled-job PDF.
- **Latent `NameError` in `infographicgenerator.py`.** `InfographicGenerator._record_tokens()`'s success-path log statement (line 621) references an undefined variable `role` (`f"role={role} type=infographic ..."`) — this is inside a bare `except Exception` block, so the resulting `NameError` is caught and logged as a warning rather than propagating, but the intended log line never actually prints correctly. Not fixed per this task's docstrings-only scope.
- **Latent `NameError` risk in `ppt_generator.py`'s import-fallback block.** When the optional `email_intelligence` module is missing, the `except ImportError` handler calls `get_logger(__name__)` (line 36) before `from logging_config import get_logger` is imported later in the file (line 48) — if this exact branch is hit, it would raise `NameError: name 'get_logger' is not defined` instead of the intended warning log. Not fixed per this task's docstrings-only scope.
- **No PDF/PPTX is ever persisted to disk.** All three artifact types exist only transiently in memory (PDF bytes / base64 PPTX) and are either returned directly as an HTTP JSON response or attached to an outbound email; nothing under `Data/` or elsewhere stores a copy. If `SMTP_USER`/`SMTP_PASS` are unset, the scheduled-job artifacts are generated, logged as `tools_completed`, and then discarded with no delivery.
