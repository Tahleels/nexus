# Jobs & Scheduler — Deep Dive

Complete technical reference for Nexus AI's scheduled-job system: the job CRUD/ownership model, the APScheduler-backed trigger engine, per-execution logging that backs the job monitor's chat-style output panel, and the HTTP surface that ties it all together.

Verified directly against code on 2026-06-29. Every file/line reference below was read, not inferred — if you change these files, this doc will drift, so re-verify before trusting it months from now.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Job Model — `job_manager.py`](#job-model)
3. [Trigger Engine — `scheduler_service.py`](#trigger-engine)
4. [Execution Pipeline — `_execute_job()`](#execution-pipeline)
5. [Token Usage Recording](#token-usage-recording)
6. [Email Delivery](#email-delivery)
7. [Execution Logging — `execution_logger.py`](#execution-logging)
8. [HTTP API — `app_jobs_routes.py`](#http-api)
9. [Ownership & Visibility Rules](#ownership--visibility-rules)
10. [Configuration Reference](#configuration-reference)
11. [Database Schema](#database-schema)
12. [Key Libraries](#key-libraries)

---

## Architecture Overview

```
                    ┌──────────────────────────────────────────────┐
                    │                  HTTP LAYER                   │
                    │     blueprints/app_jobs_routes.py (jobs_bp)    │
                    │  CRUD • run/toggle • logs • execution output   │
                    └───────────────────┬────────────────────────────┘
                                        │
                ┌───────────────────────┼────────────────────────┐
                ▼                       ▼                        ▼
   services/job_manager.py   services/scheduler_service.py   core/execution_logger.py
   JobManager (CRUD,         SchedulerService                start/append/finish
   ownership checks)         (APScheduler wrapper)            execution + log_lines
        │                          │      │                        ▲
        │ app_jobs (SQL Server)    │      │                        │
        └──────────────────────────┘      │                        │
                                            │ on_job_created/        │
                                            │ updated/deleted/        │
                                            │ toggled / run_now        │
                                            ▼                        │
                              BackgroundScheduler (UTC, 4 workers)   │
                              CronTrigger / IntervalTrigger          │
                                            │ fires                  │
                                            ▼                        │
                              _execute_job(job, scheduler) ───────────┘
                                1. Re-read job from DB
                                2. Build user_context + token_recorder
                                3. Resolve agent → NLQEngine.process_question()
                                4. For each tool in job["tools"]:
                                     dashboard / report / infographic / ppt
                                     → generate config/layout → record tokens
                                     → build PDF/PPTX bytes (artifact_builder)
                                5. Email artifacts to delivery.emails (SMTP)
                                6. execution_logger.finish_execution()
                                7. job_manager.update_run_meta()
                                8. _refresh_next_run() → app_jobs.next_run
```

---

## Job Model

**File:** [services/job_manager.py](../services/job_manager.py)

A "job" is a row in `app_jobs` bundling an agent, an NLQ prompt, a list of output tools, a schedule, and a delivery (email) config. `JobManager` (line 78) is the sole gateway to that table — every operation is a targeted per-row SQL statement (no read-all/write-all round trips), which the module docstring (lines 1-7) explains was a deliberate fix for a prior data-loss bug where two jobs finishing/being edited concurrently could clobber each other under the old DELETE-all/INSERT-all pattern.

Row shape, decoded by `_row_to_job()` (line 19):

```python
{
  "id", "name", "description", "agent_name", "nlq_prompt",
  "tools": [...],            # JSON-decoded, e.g. ["dashboard", "ppt"]
  "schedule": {...},         # JSON-decoded, see _build_trigger() below
  "delivery": {...},         # JSON-decoded, e.g. {"emails": "...", "subject": "..."}
  "enabled": bool,
  "created_by", "created_by_username", "created_by_role",
  "created_at", "updated_at", "last_run", "next_run",
  "run_count", "last_status",
}
```

Key methods on `JobManager`:

| Method | Line | Purpose |
|---|---|---|
| `load_jobs()` | 105 | All jobs, ordered by `created_at` (used by scheduler startup) |
| `save_jobs(jobs)` | 120 | Full DELETE+re-INSERT — **one-time JSON migration only**, not used in normal operation |
| `create_job(data, created_by, ...)` | 139 | New job with a fresh UUID id; rejects duplicate names (`uq_app_jobs_name` constraint → `"Job name already exists"`) |
| `get_job(job_id)` | 174 | Single job lookup |
| `get_all_jobs()` | 209 | Alias for `load_jobs()` |
| `get_jobs_for_user(user_id, role)` | 213 | `admin` → all; otherwise only jobs the user created |
| `_check_owner(job, uid, role)` | 230 | `True` for admin, or if `job["created_by"] == uid` |
| `update_job(job_id, data, ...)` | 235 | Sparse PATCH over `{name, description, agent_name, nlq_prompt, tools, schedule, delivery, enabled}`; ownership-checked |
| `delete_job(job_id, ...)` | 298 | Ownership-checked delete |
| `toggle_job(job_id, ...)` | 319 | Flips `enabled`; ownership-checked |
| `update_run_meta(job_id, status, next_run)` | 345 | Called by the scheduler after every execution — atomically increments `run_count`, sets `last_run`/`last_status`, and `COALESCE`s in a new `next_run` |

The module-level singleton `job_manager` (line 364) is the intended entry point; nothing outside this module should construct `JobManager()` directly.

---

## Trigger Engine

**File:** [services/scheduler_service.py](../services/scheduler_service.py)

`SchedulerService` (class at line 753) wraps a single process-wide `BackgroundScheduler` — UTC timezone, a 4-worker thread pool, and `job_defaults={"coalesce": True, "max_instances": 1}` so a slow or missed run never stacks duplicate executions for the same job.

### Trigger construction — `_build_trigger()`

Line 692. Reads `job["schedule"]["type"]` and builds the matching APScheduler trigger:

| `type` | Fields used | Trigger |
|---|---|---|
| `cron` | `cron` (crontab string) | `CronTrigger.from_crontab(...)` |
| `interval` | `unit`, `value`, optional `start_from` | `IntervalTrigger(**{unit: value}, start_date=...)` |
| `daily` | `hour`, `minute` (UTC) | `CronTrigger(hour=, minute=, timezone="UTC")` |
| `weekly` | `day_of_week`, `hour`, `minute` (UTC) | `CronTrigger(day_of_week=, hour=, minute=, timezone="UTC")` |
| *(anything else)* | — | Falls back to `IntervalTrigger(hours=1)` |

`_parse_start_from()` (line 679, `scheduler_service.py`) parses `schedule["start_from"]` as ISO-8601, assuming UTC if no offset is given.

### Lifecycle hooks

`SchedulerService` exposes hooks that `blueprints/app_jobs_routes.py` calls right after each `job_manager` write so the in-memory APScheduler state and the SQL-persisted job state never drift apart:

| Hook | Line | Called from (route) |
|---|---|---|
| `on_job_created(job)` | 853 | `POST /api/jobs` |
| `on_job_updated(job)` | 858 | `PUT /api/jobs/<job_id>` |
| `on_job_deleted(job_id)` | 865 | `DELETE /api/jobs/<job_id>` |
| `on_job_toggled(job_id, enabled)` | 869 | `POST /api/jobs/<job_id>/toggle` |

`start()` (line 775) starts the underlying scheduler once and calls `_reload_all_jobs()` (line 795), which loads every enabled job from SQL Server and registers it — this is how schedules survive an app restart even though APScheduler itself keeps no persistent job store here.

`run_now(job_id)` (line 883) bypasses APScheduler entirely: it spawns a daemon `threading.Thread` that calls `_execute_job()` directly, so a manual "Run now" fires immediately regardless of the configured schedule. `is_running(job_id)` (line 930) infers in-flight status from the most recent execution log row's `status == "running"`, with a 30-minute cutoff to guard against a crashed process leaving a stale "running" row forever.

---

## Execution Pipeline

**File:** [services/scheduler_service.py](../services/scheduler_service.py), function `_execute_job()` (line 341).

This single function is invoked both by the APScheduler trigger callback and by `run_now()` — it is the one true execution path for a job. Steps, with their line anchors:

1. **Re-fetch + start logging** (lines 385-389) — re-reads the job from `job_manager.get_job()` so edits since the in-memory schedule was loaded apply, then calls `execution_logger.start_execution()`.
2. **User context + token recorder** (lines 403-406) — `_make_user_context(job)` (line 182) builds `{id, username, role}` from the job's creator fields, falling back to an admin/no-record context if `created_by` is 0. `_make_token_recorder(...)` (line 201) returns a no-op callable for admin/anonymous users, or a real `token_limits.record_usage()`-backed callable otherwise.
3. **Resolve agent + run NLQ** (lines 415-426) — looks up the agent config via `_get_agent_manager()`, then calls `NLQEngine.process_question()` with the real `user_context` (not a generic "scheduler" user) so chat/schema-prune token usage is attributed correctly.
4. **Per-tool generation loop** (starting at line 449) — iterates `job["tools"]`, dispatching:
   - `dashboard` → `dashboard_generator.generate_dashboard_config()` → `artifact_builder.build_dashboard_pdf()`
   - `report` → `reportgenerator.generate_report_config()` → `artifact_builder.build_report_pdf()`
   - `infographic` → `InfographicGenerator.generate_infographic_layout()` → `artifact_builder.build_infographic_pdf()`
   - `ppt` → `_detect_client(rows)` (line 78) guesses branding from `MeetingTitle` text, optionally enriches with `email_intelligence` data if `delivery.include_email_data` is set, then `PresentationGenerator.generate()`

   Each branch records token usage via `token_recorder(...)` (lines 464, 488, 523, 597) and appends a result to `artifacts` as `{"tool", "data", "filename", "is_base64"}`. A failure in any single tool is caught, logged, and appended to the execution log — it does not abort the other tools.
5. **Email delivery** (starting at line 613) — if `job["delivery"]` has `emails` and at least one artifact was produced, sends an HTML email with all artifacts attached via `_send_email()` (line 256).
6. **Finalize** (starting at line 644) — `execution_logger.finish_execution(status="success", ...)`, `job_manager.update_run_meta(job_id, "success")` (line 659), and `_refresh_next_run()` (line 310, called at line 661) to persist APScheduler's computed next-fire time back into `app_jobs.next_run`.

Any exception anywhere in the pipeline is caught at the top level (line 669) and recorded as a `"failed"` execution — since this function always runs on a background/daemon thread, an uncaught exception would otherwise vanish silently without ever marking the job failed.

---

## Token Usage Recording

Per the module docstring (lines 4-15), every LLM call inside a scheduled job run is now attributed to the job's *creator*, not to a generic "scheduler" identity, and admin-created jobs are exempted from token accounting (`token_limits` already excludes admins):

| Call type | Recorded where |
|---|---|
| NLQ / SQL generation | Inside `nlq_engine.process_question()`, via the `user_context` passed in (call at line 426) |
| Schema pruning | Inside `nlq_engine`, same `user_context` |
| AI analysis | Via the `token_recorder` callback passed through |
| `dashboard` / `report` / `infographic` / `ppt_generate` | Recorded directly in `_execute_job()` after each tool call (lines 464, 488, 523, 597) |

`_estimate_tokens(*texts)` (line 235) is the fallback 4-chars-≈-1-token heuristic used when a generator doesn't return an explicit token count (e.g. infographic/PPT paths estimate from the summary points + a sample of rows + the response size).

---

## Email Delivery

`_send_email()` (line 256) sends an HTML email with attachments via `smtplib`, configured entirely through environment variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`). It no-ops with a warning log if `SMTP_USER`/`SMTP_PASS` aren't set — jobs still complete successfully, they just skip delivery. `_to_bytes()` (line 247) and `_fix_b64_padding()` (line 242) normalize attachment payloads, which may arrive as raw bytes or base64 strings (PPTX artifacts are base64; PDF artifacts are raw bytes).

---

## Execution Logging

**File:** [core/execution_logger.py](../core/execution_logger.py)

Backs the job monitor UI with a full per-run audit trail stored in `app_job_executions`. Per the module docstring (lines 4-13), each log entry stores both a `query_result` (the full `{sql_query, columns, data, insights, analysis}` the user would see in chat — capped at `_MAX_STORED_ROWS = 500` rows, line 27) and a lightweight `artifacts` list (`{tool, filename}` only — no binary data, which is delivered via email instead of stored in the log).

| Function | Line | Purpose |
|---|---|---|
| `start_execution(job_id, job_name, triggered_by)` | 74 | Inserts a new `"running"` row, prunes history to the most recent 100 executions per job, returns a fresh 8-char `exec_id` |
| `append_log(job_id, exec_id, message)` | 103 | Appends a `"[HH:MM:SS] message"` line to `log_lines`; called throughout `_execute_job()` to build the step-by-step progress trail |
| `finish_execution(job_id, exec_id, status, ...)` | 125 | Writes the final `status`, `duration_ms`, `error`, `tools_completed`, `artifacts`, and the full `query_result` blob; appends a "✅ Success"/"❌ Failed" log line |
| `get_logs(job_id, limit)` | 190 | Most-recent-first executions **without** `query_result` (for list views) |
| `get_execution(job_id, exec_id)` | 209 | Single execution **with** `query_result` — backs the monitor's Output panel |
| `get_all_recent_logs(job_ids, limit)` | 228 | Combined recent-activity feed across every job a user can see |

`_clean_nan()` (line 30) recursively sanitizes `NaN` floats, `datetime`/`date`, and `Decimal` values before JSON serialization — necessary because NLQ result rows can contain any of these from raw SQL Server data.

---

## HTTP API

**File:** [blueprints/app_jobs_routes.py](../blueprints/app_jobs_routes.py)

| Route | Method | Notes |
|---|---|---|
| `/api/jobs` | GET | List jobs visible to the current user (`_get_jobs_for_user_with_org`, line 27), enriched with live `next_run`/`is_running` and org (department/project) metadata |
| `/api/jobs` | POST | Create (dev/admin); schedules immediately via `on_job_created` |
| `/api/jobs/<job_id>` | GET | Get one job (owner or admin) |
| `/api/jobs/<job_id>` | PUT | Update (dev/admin, owner-checked); reschedules via `on_job_updated` |
| `/api/jobs/<job_id>` | DELETE | Delete (dev/admin, owner-checked); unschedules via `on_job_deleted` first |
| `/api/jobs/<job_id>/run` | POST | "Run now" (dev/admin, owner-checked) → `scheduler_service.run_now()` |
| `/api/jobs/<job_id>/toggle` | POST | Enable/disable (dev/admin, owner-checked) → `scheduler_service.on_job_toggled()` |
| `/api/jobs/logs` | GET | Recent executions across all visible jobs (`?limit=`) |
| `/api/jobs/logs/<job_id>` | GET | Recent executions for one job (owner or admin, `?limit=`) |
| `/api/jobs/logs/<job_id>/<exec_id>/output` | GET | Full `query_result` for one execution — **the chat-style output panel** the module docstring (lines 4-7) calls out: renders the same table + SQL + insights + AI analysis view the user sees when chatting |
| `/jobs` | GET (page) | Jobs management page, registered via `register_jobs_routes()` (line 264) |
| `/job-monitor` | GET (page) | Job monitor page, same registration function |

`register_jobs_routes(app)` (line 277) is called once from `app.py` to mount `jobs_bp` and add the two page routes.

---

## Ownership & Visibility Rules

`_get_jobs_for_user_with_org()` (line 27, in `app_jobs_routes.py`) implements the visibility model used by both `/api/jobs` and `/api/jobs/logs`:

- **Admins** see every job, unfiltered.
- **Non-admins** see a job if it shares at least one department or project with the user (via `org_db.get_resources_org('app_jobs')` / `org_db.get_user_org_assignments(user_id)`), **or** if the job is unscoped (no dept/project assigned) and they are its creator.

Separately, `JobManager._check_owner()` (line 230) governs *mutation* rights (update/delete/toggle/run): admins can act on any job, everyone else only on jobs they created — this is a stricter, simpler check than the read-visibility rule above and does not consider department/project sharing.

---

## Configuration Reference

| Variable | Default | Effect |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | Outgoing mail server for job delivery emails |
| `SMTP_PORT` | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` / `SMTP_PASS` | — | SMTP credentials; delivery is skipped (with a warning log) if unset |
| `SMTP_FROM` | `SMTP_USER` | Override the From address |
| `OPENAI_API_KEY` | — | Passed to `NLQEngine` for SQL generation during job execution |

Scheduler internals (worker pool size, misfire grace time, max instances) are hardcoded in `SchedulerService.__init__` and `_add_to_scheduler()` rather than environment-configurable — see lines 753-814 of `scheduler_service.py` to change them.

---

## Database Schema

All in SQL Server (`nexus` DB):

| Table | Key columns |
|---|---|
| `app_jobs` | id (PK, UUID), name (unique), description, agent_name, nlq_prompt, tools (JSON), schedule (JSON), delivery (JSON), enabled, created_by/username/role, created_at, updated_at, last_run, next_run, run_count, last_status |
| `app_job_executions` | exec_id, job_id, job_name, triggered_by, status, started_at, finished_at, duration_ms, error, tools_completed (JSON), row_count, artifacts (JSON), query_result (JSON), log_lines (JSON) |

`app_jobs` is read/written exclusively through `services/job_manager.py`; `app_job_executions` exclusively through `core/execution_logger.py`. Per-job history in `app_job_executions` is capped at the 100 most recent rows (enforced in `start_execution()`).

---

## Key Libraries

| Library | Install | Used for |
|---|---|---|
| `APScheduler` | `pip install apscheduler` | **Required.** Cron/interval trigger engine (`BackgroundScheduler`, `CronTrigger`, `IntervalTrigger`) |
| `pandas` | — | Building a DataFrame from NLQ result rows before tool generation |
| `pyodbc` (via `app_db`) | — | SQL Server connections for `app_jobs` / `app_job_executions` |
| `smtplib` / `email.mime.*` | stdlib | Job delivery emails with PDF/PPTX attachments |
