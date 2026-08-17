# SLM Training Pipeline — Deep Dive

Complete technical reference for Nexus AI's training-data pipeline: capturing instruction/output pairs from both Workspace chat and BI tool usage, scoring their quality, routing them through a human review workflow, and exporting them as fine-tuning data for Nexus AI's own small language models (SLMs).

Verified directly against code on 2026-06-29. Every file/line reference below was read, not inferred — if you change these files, this doc will drift, so re-verify before trusting it months from now.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Two Parallel Pipelines, One Shared Design](#two-parallel-pipelines-one-shared-design)
3. [Chat Training Pairs — `training_db.py`](#chat-training-pairs)
4. [BI Training Pairs — `bi_training_db.py`](#bi-training-pairs)
5. [Quality Scoring Compared](#quality-scoring-compared)
6. [Review Workflow](#review-workflow)
7. [Review UI & Export — `training_bp.py`](#review-ui--export)
8. [Export Formats](#export-formats)
9. [Configuration Reference](#configuration-reference)
10. [Database Schema](#database-schema)
11. [Key Libraries](#key-libraries)

---

## Architecture Overview

```
  WORKSPACE CHAT                                   BI TOOLS
  ws_messages / ws_feedback /                       (query, dashboard, report,
  ws_citations / ws_artifacts /                      infographic, presentation,
  ws_retry_log                                       presentation_optimize)
       │                                                  │
       │ extract_pairs_from_conversation()                │ save_async(tool_type, user,
       │ (pull user→assistant turns)                      │   instruction, output, ...)
       ▼                                                  ▼
  database/training_db.py                          database/bi_training_db.py
    compute_quality_score()                          compute_bi_quality_score()
      base 50 + feedback/edit/retry/                   base 50 + artifact/length/
      length/citations/artifact signals                 sql/complex-tool/feedback signals
    score_to_status()                                 score_to_status()
      >=75 auto_approved, >=40 pending,                 >=70 auto_approved, >=40 pending,
      else needs_review                                 else needs_review
    insert_pair() ─────────┐                          insert_bi_pair() (fire-and-forget
                            │                            daemon thread, never blocks the
                            ▼                            BI route's HTTP response)
                  ws_training_pairs                            │
                  ws_training_preference_pairs                 ▼
                  (DPO chosen/rejected)              bi_training_pairs
                            │                                  │
                            └──────────────┬───────────────────┘
                                           ▼
                         blueprints/training_bp.py  (chat pairs only — see note)
                           GET  /workspace/training        — review UI page
                           GET  /api/.../pairs              — paginated list
                           POST /api/.../pairs/<id>/review   — approve/reject/tag
                           POST /api/.../pairs/bulk-review    — batch approve/reject
                           GET  /api/.../stats               — KPI dashboard
                           GET  /api/.../export              — JSONL download
                                           │
                                           ▼
                         _build_alpaca / _build_chatml /
                         _build_openai_ft / _build_dpo
                                           │
                                           ▼
                         training_{format}_{N}pairs.jsonl
                         + ws_training_exports audit row
```

---

## Two Parallel Pipelines, One Shared Design

Nexus AI captures fine-tuning data from two independent sources that never overlap, using two parallel modules that intentionally mirror each other:

| | `database/training_db.py` | `database/bi_training_db.py` |
|---|---|---|
| Source | Workspace chat conversations (`ws_messages`) | BI tool invocations (query, dashboard, report, infographic, presentation, optimize) |
| Capture trigger | Explicit/batch extraction (`auto_extract_and_insert`) after the fact | Live, fire-and-forget (`save_async`) at the moment of generation |
| Storage table | `ws_training_pairs` | `bi_training_pairs` |
| Quality base score | 50 | 50 |
| Auto-approve threshold | score >= 75 | score >= 70 |
| Connection helper | `app_db.get_app_db` (aliased `_get_db`) | Same — `app_db.get_app_db` (aliased `_get_db`) |
| Review states | `pending` / `auto_approved` / `approved` / `needs_review` / `rejected` | Same five states |
| Export | Via `blueprints/training_bp.py` JSONL builders | *(no dedicated export blueprint in this module's scope — see note below)* |

The `bi_training_db.py` module docstring (lines 1-16 of the file) states this explicitly: it "mirrors the pattern of `database/training_db.py` — same connection helper, same quality scoring approach, same review workflow," differing mainly in *when* pairs are captured (live vs. extracted) and in the BI-specific fields (`tool_type`, `sql_query`, `agent_name`) it tracks instead of chat-specific ones (`message_id`, `has_citations`, `is_edited`).

**Note on `bi_training_db` callers:** `insert_bi_pair`/`save_async` are imported and invoked from `app.py` (outside this doc's file scope — `app.py` does `import bi_training_db as _bi_td`), not from `blueprints/training_bp.py`. `training_bp.py` only serves the chat-pairs (`training_db`) review UI and export; there is no BI-pairs equivalent blueprint among the files covered here.

---

## Chat Training Pairs

**File:** [database/training_db.py](../database/training_db.py)

### Quality scoring

`compute_quality_score()` (line 35) returns `(score: float 0-100, breakdown: dict)`:

| Signal | Points | Rationale |
|---|---|---|
| Base | +50 | Starting point |
| `thumbs_up` | +25 | Explicit human approval |
| `thumbs_down` | -30 | Explicit human rejection |
| `is_edited` | -15 | User had to fix the response |
| `retry_count > 0` | -10 | User tried again (implicit rejection) |
| `response_length > 500` | +10 | Length signals useful depth |
| `has_citations` | +5 | Grounded / sourced answer |
| `has_artifact` | +5 | Response produced actionable output |

`score_to_status()` (line 93) buckets the result: `>= 75` → `auto_approved`, `>= 40` → `pending`, else `needs_review`.

### Extraction from chat history

- `extract_pairs_from_conversation(conversation_id)` (line 113) — pulls `ws_messages` rows for a conversation (role in `user`/`assistant`, content length > 10), pairs up consecutive user→assistant turns.
- `_get_feedback_for_message()` (line 143), `_has_citations()` (line 154), `_has_artifact()` (line 164), `_retry_count()` (line 174) — small per-message lookups against `ws_feedback`, `ws_citations`, `ws_artifacts`, `ws_retry_log` respectively, used to assemble the scoring inputs.
- `auto_extract_and_insert(conversation_id, workspace_id)` (line 184) — orchestrates the above: extracts pairs, skips any whose `message_id` was already extracted (dedup against `ws_training_pairs`), scores and inserts the rest via `insert_pair()`. Returns the count of newly inserted pairs.

### CRUD

| Function | Line | Purpose |
|---|---|---|
| `insert_pair(...)` | 251 | Insert one pair into `ws_training_pairs` (text fields truncated: instruction/output to 8000 chars, context to 4000) |
| `get_pairs(...)` | 339 | Paginated, filterable list (`status`, `domain`, `workspace_id`, score range) for the review UI, ordered by quality desc then recency |
| `update_pair_status(pair_id, status, reviewed_by, notes)` | 415 | Approve/reject a pair, stamping `reviewed_by`/`reviewed_at` |
| `update_pair_domain_tags(pair_id, domain, tags)` | 438 | The "tag" review action |
| `delete_pair(pair_id)` | 456 | Deletes the pair and any rows in `ws_training_annotations` |
| `get_pair_by_id(pair_id)` | 469 | Full single-row fetch with reviewer username joined in |

### Preference (DPO) pairs

`insert_preference_pair(...)` (line 501) and `get_preference_pairs(page, per_page)` (line 555) manage `ws_training_preference_pairs` — chosen-vs-rejected output pairs for the same prompt, used to build `dpo_jsonl` exports. These are a separate concept from the approve/reject status on regular pairs; preference pairs have their own `status` column (excluded from list results when `'rejected'`).

### Stats & export bookkeeping

- `get_training_stats()` (line 594) — KPI dict: per-status counts, average/max/min quality (rounded to 1 decimal), `export_ready` (approved + auto_approved), `preference_pairs` count, `total_exports` count, and a `by_domain` breakdown.
- `get_export_history(limit)` (line 649) / `log_export(...)` (line 680) — read/write `ws_training_exports`, an audit log of every JSONL download (format, pair/preference counts, filters used, file name/size, SHA-256 content hash).
- `get_approved_pairs_for_export(domain, min_score, include_auto)` (line 734) / `get_approved_preference_pairs_for_export(domain)` (line 778) — the actual export queries, restricted to `approved` (+ optionally `auto_approved`) status.

---

## BI Training Pairs

**File:** [database/bi_training_db.py](../database/bi_training_db.py)

### Valid tool types

`_VALID_TOOL_TYPES` (line 18): `bi_query`, `bi_dashboard`, `bi_report`, `bi_infographic`, `bi_presentation`, `bi_presentation_optimize`. `insert_bi_pair()` rejects (logs a warning, returns 0) any `tool_type` outside this set.

### Quality scoring

`compute_bi_quality_score()` (line 32) returns `(score: float 0-100, breakdown: dict)`:

| Signal | Points | Rationale |
|---|---|---|
| Base | +50 | Starting point |
| `has_artifact` | +5 | BI tools always produce structured output (fixed bonus, not conditional) |
| `output_length > 1000` | +10 | Rich JSON response |
| `has_sql_query` | +5 | Concrete SQL deliverable (mainly `bi_query`) |
| `tool_type` in `(bi_presentation, bi_presentation_optimize, bi_query)` | +5 | "Complex tool" — these are the highest-value pairs |
| `thumbs_up` | +15 | |
| `thumbs_down` | -20 | |

`score_to_status()` (line 89) buckets: `>= 70` → `auto_approved` (lower than chat's 75, reflecting that BI outputs start from a higher quality baseline since they're always structured), `>= 40` → `pending`, else `needs_review`.

### Capture

| Function | Line | Purpose |
|---|---|---|
| `insert_bi_pair(...)` | 114 | Synchronous insert into `bi_training_pairs`; validates `tool_type`, truncates text fields, logs the result |
| `save_async(...)` | 186 | **The actual integration point.** Computes the quality score, then spawns a daemon thread to call `insert_bi_pair()` — returns immediately so it never adds latency to the BI tool's HTTP response. This is the function BI tool routes call after a successful generation. |

### Read / update / delete

| Function | Line | Purpose |
|---|---|---|
| `get_bi_pairs(...)` | 240 | Paginated, filterable (`tool_type`, `status`, `agent_name`, score range) list, ordered by quality desc then recency |
| `get_bi_pair_by_id(pair_id)` | 316 | Full single-row fetch with reviewer username joined in |
| `update_bi_pair_status(...)` | 348 | Approve/reject, stamping reviewer/timestamp |
| `update_bi_pair_domain_tags(...)` | 371 | Tag action |
| `delete_bi_pair(pair_id)` | 389 | Hard delete |

### Stats & export

- `get_bi_stats()` (line 405) — same shape as the chat stats, plus BI-specific `by_tool` and `by_agent` breakdowns instead of `by_domain`.
- `get_approved_bi_pairs_for_export(tool_type, min_score, include_auto)` (line 465) — mirrors `training_db.get_approved_pairs_for_export()` but includes the `sql_query` column and filters by `tool_type` instead of `workspace_id`.

---

## Quality Scoring Compared

Both scorers clamp the final score to `[0, 100]` and share the same base (50) and bucket logic shape, but weight signals differently because the two domains behave differently — chat responses are penalized for edits/retries (signals that are meaningless for BI tool calls, which don't have a retry/edit concept in this schema), while BI pairs get a flat artifact bonus and a "complex tool" bonus that chat pairs have no equivalent for.

| | Chat (`training_db.compute_quality_score`) | BI (`bi_training_db.compute_bi_quality_score`) |
|---|---|---|
| Auto-approve cutoff | 75 | 70 |
| Negative signals | `is_edited` (-15), `retry` (-10), `thumbs_down` (-30) | `thumbs_down` (-20) only |
| Positive signals | `thumbs_up` (+25), `long_response` (+10), `has_citations` (+5), `has_artifact` (+5) | `has_artifact` (+5, unconditional), `long_output` (+10), `has_sql_query` (+5), `complex_tool` (+5), `thumbs_up` (+15) |

---

## Review Workflow

Both pipelines share the same five-state lifecycle:

```
   insert (scored) ──► auto_approved ───────────────┐
        │                                            │
        ├──► pending ──► (human review) ──► approved ┼──► export-eligible
        │                       │                    │
        └──► needs_review ──────┴──────────────────► rejected (excluded from export)
```

A human reviewer (admin or dev role) acts on `pending`/`needs_review` pairs through the review UI, which calls `update_pair_status()` / `update_bi_pair_status()` to move a pair to `approved` or `rejected`, optionally with notes. `auto_approved` and `approved` are both export-eligible (`export_ready` stat = their sum); `rejected` pairs are excluded from every export query and from `get_preference_pairs()`'s default listing.

---

## Review UI & Export

**File:** [blueprints/training_bp.py](../blueprints/training_bp.py)

Covers chat training pairs (`training_db`) only — see the note in [Two Parallel Pipelines](#two-parallel-pipelines-one-shared-design).

| Route | Method | Line | Notes |
|---|---|---|---|
| `/workspace/training` | GET | 74 | Main review page; pre-loads `get_training_stats()` + 10 most recent exports |
| `/api/workspace/training/extract` | POST | 99 | Auto-extract pairs from one conversation, or every conversation (optionally scoped to a workspace) via `extract_all` |
| `/api/workspace/training/pairs` | GET | 166 | Paginated/filtered pair list for the review grid |
| `/api/workspace/training/pairs/<id>/review` | POST | 208 | `action`: `approve` / `reject` / `tag` |
| `/api/workspace/training/pairs/bulk-review` | POST | 247 | Approve/reject up to 500 pair ids in one call; per-id failures are logged and skipped, not fatal |
| `/api/workspace/training/pairs/<id>` | DELETE | 290 | Hard delete |
| `/api/workspace/training/stats` | GET | 304 | KPI dashboard data |
| `/api/workspace/training/export` | GET | 410 | JSONL download — see [Export Formats](#export-formats) |
| `/api/workspace/training/export-history` | GET | 501 | 50 most recent export audit rows |
| `/api/workspace/training/preference-pairs` | GET | 515 | Paginated DPO preference-pair list |

Access control: `_require_login()` (line 47) gates any-logged-in-user routes (list/stats/export-history/preference-pairs); `_require_admin_or_dev()` (line 55) gates mutating routes (extract/review/bulk-review/delete/export) to `admin`/`dev` roles only.

---

## Export Formats

`EXPORT_FORMATS` (line 317): `alpaca_jsonl`, `chatml_jsonl`, `openai_ft_jsonl`, `dpo_jsonl`. Each maps to a builder function that turns DB rows into JSONL lines (one JSON object per line):

| Format | Builder | Line | Shape |
|---|---|---|---|
| `alpaca_jsonl` | `_build_alpaca()` | 320 | `{"instruction", "input", "output", "domain"?}` |
| `chatml_jsonl` | `_build_chatml()` | 342 | `{"messages": [{"role": "system"\|"user"\|"assistant", "content"}]}` |
| `openai_ft_jsonl` | `_build_openai_ft()` | 362 | Same `{"messages": [...]}` shape as `chatml_jsonl` — kept as a separately labeled format in the export UI even though today the payloads are identical |
| `dpo_jsonl` | `_build_dpo()` | 387 | `{"prompt", "chosen", "rejected", "domain"?}`, sourced from `ws_training_preference_pairs` instead of `ws_training_pairs` |

`api_export()` (line 410) resolves `?format=`, `?domain=`, `?min_score=`, `?include_auto=` from the query string, fetches the matching rows (`get_approved_pairs_for_export()` for the first three formats, `get_approved_preference_pairs_for_export()` for `dpo_jsonl`), joins the JSONL lines, computes a SHA-256 content hash, logs the export via `log_export()`, records a token-usage entry for the requester, and streams the file back with `Content-Disposition: attachment` and an `X-Pair-Count` header.

---

## Configuration Reference

Neither module reads dedicated environment variables — both rely entirely on the shared SQL Server connection configured for `app_db.get_app_db` (see `config.py`, imported but only indirectly used by both `training_db.py` and `bi_training_db.py` for shared app configuration).

| Constant | File:line | Effect |
|---|---|---|
| Chat auto-approve threshold | `database/training_db.py:93` (`score_to_status`) | Score >= 75 → `auto_approved` |
| BI auto-approve threshold | `database/bi_training_db.py:89` (`score_to_status`) | Score >= 70 → `auto_approved` |
| Bulk review cap | `blueprints/training_bp.py:275` (`ids[:500]`) | Max ids processed per bulk-review call |
| Pair-list page size cap | `blueprints/training_bp.py` (`api_pairs`, `per_page` clamp) | Max 200 per page |
| Preference-pair page size cap | `blueprints/training_bp.py` (`api_preference_pairs`, `per_page` clamp) | Max 100 per page |

---

## Database Schema

All in SQL Server (`nexus` DB), accessed via `app_db.get_app_db`:

| Table | Key columns |
|---|---|
| `ws_training_pairs` | id (PK), message_id, conversation_id, workspace_id, instruction, context, output, quality_score, quality_breakdown (JSON), status, domain, tags, language, model_used, token_count, has_citations, has_artifact, is_edited, feedback_rating, retry_count, reviewed_by, reviewed_at, review_notes, created_at, updated_at |
| `ws_training_preference_pairs` | id (PK), base_pair_id, conversation_id, prompt, chosen_output, rejected_output, chosen_score, rejected_score, preference_source, domain, tags, status, created_at |
| `ws_training_exports` | id (PK), exported_by (FK users), export_format, pair_count, preference_count, filters (JSON), file_name, file_size_bytes, content_hash, exported_at |
| `ws_training_annotations` | pair_id (FK, cascaded on delete by `delete_pair`) |
| `bi_training_pairs` | id (PK), tool_type, user_id, agent_name, instruction, context, output, sql_query, quality_score, quality_breakdown (JSON), status, model_used, token_count, feedback_rating, domain, tags, reviewed_by, reviewed_at, review_notes, created_at, updated_at |
| `ws_messages` / `ws_feedback` / `ws_citations` / `ws_artifacts` / `ws_retry_log` | Source tables read (not written) by `training_db.py`'s extraction helpers — owned by the Workspace chat subsystem |
| `users` | Joined for `reviewed_by_name` / `exported_by_name` display fields |

---

## Key Libraries

| Library | Used for |
|---|---|
| `pyodbc` (via `app_db.get_app_db`) | All SQL Server connections in both modules |
| `json` | Encoding/decoding `quality_breakdown`, `filters`, and JSONL export lines |
| `hashlib` | SHA-256 content hash of exported files (`training_bp.api_export`) |
| `threading` | Daemon thread for `bi_training_db.save_async`'s fire-and-forget insert |
| `flask` (`Blueprint`, `Response`, `jsonify`, `render_template`) | `training_bp.py`'s HTTP surface and JSONL file download response |
