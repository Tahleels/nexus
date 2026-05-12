"""
scheduler_service.py — Nexus AI · Jobs & Scheduler  (v7)

Change from v6:
- Token recording against the job's creator for EVERY LLM call:
    • NLQ / SQL generation   → via user_context passed to nlq_engine
    • AI analysis            → via token_recorder callback
    • Schema pruning         → via token_recorder inside nlq_engine
    • Dashboard generation   → recorded here after LLM call
    • Report generation      → recorded here after LLM call
    • Infographic generation → recorded here after LLM call
    • PPT generation         → recorded here after LLM call
- Admin users are excluded from recording (token_limits respects that)
- All previous fixes retained (PDF artifacts, analysis sync, email
  padding, client detection, is_running timeout, start_from anchoring)
"""

from logging_config import get_logger
import os
import json
import threading
from datetime  import datetime, timezone
from typing    import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler

import execution_logger as el
from job_manager     import job_manager
from artifact_builder import (
    build_dashboard_pdf,
    build_report_pdf,
    build_infographic_pdf,
)
from generator_utils import clean_nan
from email_delivery import _send_email
from trigger_builder import _build_trigger
from job_execution_helpers import (
    _get_nlq_engine, _get_agent_manager, _get_dashboard_gen,
    _get_report_gen, _get_infographic_gen, _get_ppt_gen,
    _load_clients, _detect_client,
    _make_user_context, _make_token_recorder, _estimate_tokens,
)

logger = get_logger(__name__)


# ── next_run refresh ───────────────────────────────────────────────────────────

def _refresh_next_run(scheduler, job_id: str) -> None:
    """Persist APScheduler's computed next-fire time onto ``app_jobs.next_run``.

    APScheduler tracks ``next_run_time`` in memory; this copies it into SQL
    Server so the value survives process restarts and is visible to the
    ``/api/jobs`` list endpoint without querying the live scheduler.

    Args:
        scheduler: The `BackgroundScheduler` instance holding the job.
        job_id: Job UUID to refresh.

    Returns:
        None. Errors are caught and logged, never raised.
    """
    try:
        apjob = scheduler.get_job(job_id)
        if apjob and apjob.next_run_time:
            from app_db import get_app_db
            with get_app_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE app_jobs SET next_run=? WHERE id=?",
                    apjob.next_run_time.isoformat(), job_id,
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"  next_run refresh failed for {job_id}: {e}")


# ── Per-tool handlers ──────────────────────────────────────────────────────────
# Each handler generates one job "tool" artifact and returns an artifact dict
# (see `_execute_job`'s `artifacts` list shape) or None if generation produced
# nothing (not a failure — e.g. the infographic/ppt generators can legitimately
# return no output). Extracted out of `_execute_job`'s tool loop in Phase 3
# Slice 6 so each branch's shared-state dependencies are explicit parameters
# instead of implicit closures.

def _run_dashboard_tool(df, rows, job_name, token_recorder) -> Optional[Dict]:
    """Generate the dashboard artifact for one job tool-run."""
    # generate_dashboard_config returns (config, tokens_used, input_tokens, output_tokens)
    result_cfg = _get_dashboard_gen()(df)
    if isinstance(result_cfg, tuple):
        cfg     = result_cfg[0]
        tok     = result_cfg[1] if len(result_cfg) > 1 else 0
        in_tok  = result_cfg[2] if len(result_cfg) > 2 else 0
        out_tok = result_cfg[3] if len(result_cfg) > 3 else 0
    else:
        cfg, tok, in_tok, out_tok = result_cfg, _estimate_tokens(str(result_cfg)), 0, 0

    token_recorder("dashboard", max(1, tok), in_tok, out_tok, model="gpt-4o")

    pdf_bytes = build_dashboard_pdf(
        config   = clean_nan(cfg),
        raw_data = clean_nan(rows),
    )
    return {
        "tool":      "dashboard",
        "data":      pdf_bytes,
        "filename":  f"dashboard_{job_name}.pdf",
        "is_base64": False,
    }


def _run_report_tool(df, rows, job_name, token_recorder) -> Optional[Dict]:
    """Generate the report artifact for one job tool-run."""
    # generate_report_config returns (config, tokens_used, input_tokens, output_tokens)
    result_rep = _get_report_gen()(df)
    if isinstance(result_rep, tuple):
        cfg     = result_rep[0]
        tok     = result_rep[1] if len(result_rep) > 1 else 0
        in_tok  = result_rep[2] if len(result_rep) > 2 else 0
        out_tok = result_rep[3] if len(result_rep) > 3 else 0
    else:
        cfg, tok, in_tok, out_tok = result_rep, _estimate_tokens(str(result_rep)), 0, 0

    token_recorder("report", max(1, tok), in_tok, out_tok, model="gpt-4o-mini")

    pdf_bytes = build_report_pdf(
        report_config = cfg,
        raw_data      = clean_nan(rows),
    )
    return {
        "tool":      "report",
        "data":      pdf_bytes,
        "filename":  f"report_{job_name}.pdf",
        "is_base64": False,
    }


def _run_infographic_tool(rows, insights, job_name, user_context, agent_name,
                          token_recorder) -> Optional[Dict]:
    """Generate the infographic artifact for one job tool-run."""
    summary_pts = [i.get("message", "") for i in insights]

    # InfographicGenerator.generate_infographic_layout self-records
    # real token usage internally when `user=` is accepted (see
    # InfographicGenerator._record_tokens) — don't also record an
    # estimate here, or usage gets counted twice against quota.
    # Only fall back to the scheduler's own estimate-based
    # recorder when the generator doesn't support `user=` at all,
    # so usage isn't silently dropped on that fallback path.
    _ig_self_recorded = True
    try:
        ig = _get_infographic_gen().generate_infographic_layout(
            summary_pts, rows,
            user=user_context, agent_name=agent_name,
        )
    except TypeError:
        _ig_self_recorded = False
        ig = _get_infographic_gen().generate_infographic_layout(
            summary_pts, rows
        )

    if not ig:
        return None

    if not _ig_self_recorded:
        # Estimate tokens: summary + first 10 rows as proxy for LLM prompt
        tok = _estimate_tokens(
            " ".join(summary_pts),
            json.dumps(rows[:10], default=str),
            json.dumps(ig, default=str),
        )
        token_recorder("infographic", max(1, tok), model="gpt-4o-mini")

    pdf_bytes = build_infographic_pdf(ig)
    return {
        "tool":      "infographic",
        "data":      pdf_bytes,
        "filename":  f"infographic_{job_name}.pdf",
        "is_base64": False,
    }


def _run_ppt_tool(job, job_id, exec_id, job_name, rows, insights, user_context,
                  token_recorder) -> Optional[Dict]:
    """Generate the PPT artifact for one job tool-run, including the
    optional email-intelligence context fetch."""
    client_cfg  = _detect_client(rows) or {}
    summary_pts = [i.get("message", "") for i in insights]

    # Fetch email intelligence if configured on this job
    ppt_email_text = ""
    delivery_cfg   = job.get("delivery", {})
    if delivery_cfg.get("include_email_data") and delivery_cfg.get("email_senders"):
        try:
            from email_intelligence import (
                get_emails, parse_date_range_from_question,
                format_emails_for_llm, resolve_client_db_cfg,
            )
            from database_manager import db_manager as _dm
            client_name = (
                delivery_cfg.get("detected_client_name")
                or client_cfg.get("name", "")
            )
            client_id = (
                delivery_cfg.get("detected_client_id")
                or client_cfg.get("id", "")
            )
            email_db_cfg = resolve_client_db_cfg(
                (_load_clients().get(client_id) or {}).get("email_db_config", {}),
                _dm,
            ) if client_id else {}
            date_from, date_to = parse_date_range_from_question(
                job.get("nlq_prompt", "")
            )
            emails = get_emails(
                client_name,
                delivery_cfg["email_senders"],
                date_from, date_to,
                db_cfg=email_db_cfg or None,
                client_id=client_id,
            )
            ppt_email_text = format_emails_for_llm(emails)
            if ppt_email_text:
                el.append_log(job_id, exec_id,
                    f"  Email intel: {len(emails)} emails for '{client_name}'")
        except Exception as email_err:
            logger.warning(f"PPT email fetch failed: {email_err}")
            el.append_log(job_id, exec_id, f"  Email fetch skipped: {email_err}")

    # PresentationGenerator.generate self-records real token usage
    # internally when `user=` is accepted (see
    # PresentationGenerator._record_ppt_tokens) — don't also
    # record an estimate here, or usage gets counted twice
    # against quota. Only fall back to the scheduler's own
    # estimate-based recorder when the generator doesn't support
    # `user=` at all, so usage isn't silently dropped on that
    # fallback path.
    _ppt_self_recorded = True
    try:
        ppt_result = _get_ppt_gen().generate(
            summary_pts, rows, client_cfg,
            user=user_context,
            email_text=ppt_email_text,
        )
    except TypeError:
        _ppt_self_recorded = False
        ppt_result = _get_ppt_gen().generate(
            summary_pts, rows, client_cfg
        )

    b64 = ppt_result.get("pptx_base64")
    if not b64:
        return None

    if not _ppt_self_recorded:
        # Estimate tokens from prompt proxy
        tok = _estimate_tokens(
            " ".join(summary_pts),
            json.dumps(rows[:14], default=str),
            b64[:2000],  # rough proxy for response size
        )
        token_recorder("ppt_generate", max(1, tok),
                       model=os.getenv("OPENAI_PPT_MODEL", "gpt-4o"))

    return {
        "tool":      "ppt",
        "data":      b64,
        "filename":  f"presentation_{job_name}.pptx",
        "is_base64": True,
    }


# ── Core execution pipeline ────────────────────────────────────────────────────

def _execute_job(job: Dict, scheduler=None) -> None:
    """Run one job end-to-end: NLQ query, tool generation, email delivery, logging.

    This is the single execution pipeline invoked by both the APScheduler
    trigger callback (`SchedulerService._add_to_scheduler`) and "Run now"
    (`SchedulerService.run_now`). Steps:

      1. Re-fetch the job from the DB (picks up edits since the in-memory
         schedule was loaded) and start an execution log via
         `execution_logger.start_execution`.
      2. Build a ``user_context`` from the job's creator and a no-op-unless-
         real-user token recorder (`_make_user_context`, `_make_token_recorder`).
      3. Resolve the agent config and run the NLQ engine
         (`NLQEngine.process_question`) to get rows/columns/insights/SQL.
      4. For each tool in ``job["tools"]`` (``dashboard``, ``report``,
         ``infographic``, ``ppt``), generate the artifact, record its token
         usage, and build the corresponding PDF/PPTX bytes.
      5. If the job has delivery recipients and at least one artifact was
         produced, email the artifacts as attachments via `_send_email`.
      6. Write the final execution record via `execution_logger.finish_execution`,
         update the job's run metadata (`job_manager.update_run_meta`), and
         refresh ``next_run`` (`_refresh_next_run`).

    Any exception during the run is caught, logged, and recorded as a
    ``"failed"`` execution rather than propagating — this function is always
    invoked from a background thread (scheduler worker or `run_now`'s daemon
    thread), so an uncaught exception would otherwise be silently swallowed
    without ever marking the job as failed.

    Args:
        job: Job dict (see `services.job_manager._row_to_job`) describing the
            agent, NLQ prompt, tools, and delivery settings to run.
        scheduler: The `BackgroundScheduler` instance, passed through so
            `_refresh_next_run` can read the just-computed next fire time.
            ``None`` when invoked outside the scheduler context.

    Returns:
        None. All results are side effects (execution log row, job run
        metadata, optional email).
    """
    job_id   = job["id"]
    job_name = job["name"]

    # Re-read from disk so edits since last schedule-load apply
    fresh = job_manager.get_job(job_id)
    if fresh:
        job = fresh

    exec_id = el.start_execution(job_id, job_name, triggered_by="scheduler")

    try:
        import pandas as pd
        import uuid as _uuid

        # ── Build user context from job creator ───────────────────────────
        user_context  = _make_user_context(job)
        agent_name    = job.get("agent_name", "")
        question      = job.get("nlq_prompt", "")
        token_recorder = _make_token_recorder(user_context, agent_name, question)

        uid = user_context.get("id", 0)
        if uid:
            el.append_log(job_id, exec_id,
                          f"Token recording active for user: {user_context.get('username')} (id={uid})")
        else:
            el.append_log(job_id, exec_id, "Token recording skipped — no creator user attached")

        # 1. Resolve agent
        el.append_log(job_id, exec_id, f"Resolving agent: {agent_name}")
        agent_config = _get_agent_manager().get_agent(agent_name)
        if not agent_config:
            raise ValueError(f"Agent '{agent_name}' not found")
        connection_name = agent_config["database_connection"]

        # 2. Execute NLQ — pass real user_context so engine records chat/prune tokens
        nlq_session_id = str(_uuid.uuid4())
        el.append_log(job_id, exec_id, f"Executing NLQ: {question[:80]}…")

        result = _get_nlq_engine().process_question(
            question        = question,
            agent_config    = agent_config,
            connection_name = connection_name,
            session_id      = nlq_session_id,
            user_context    = user_context,   # ← real user, not scheduler/admin
        )

        if not result.get("success"):
            raise ValueError(f"NLQ failed: {result.get('error', 'unknown')}")

        rows     = result.get("data",      [])
        columns  = result.get("columns",   [])
        insights = result.get("insights",  [])
        sql_q    = result.get("sql_query", "")
        el.append_log(job_id, exec_id,
                      f"NLQ returned {len(rows)} rows, {len(columns)} columns")

        # 3. Generate tools → PDF / PPTX, record tokens per tool
        df         = pd.DataFrame(rows) if rows else pd.DataFrame()
        tools_done: List[str]  = []
        artifacts:  List[Dict] = []

        for tool in job.get("tools", []):
            try:
                el.append_log(job_id, exec_id, f"Generating {tool}…")

                artifact = None
                if tool == "dashboard":
                    artifact = _run_dashboard_tool(df, rows, job_name, token_recorder)
                elif tool == "report":
                    artifact = _run_report_tool(df, rows, job_name, token_recorder)
                elif tool == "infographic":
                    artifact = _run_infographic_tool(
                        rows, insights, job_name, user_context, agent_name, token_recorder)
                elif tool == "ppt":
                    artifact = _run_ppt_tool(
                        job, job_id, exec_id, job_name, rows, insights, user_context, token_recorder)

                if artifact:
                    artifacts.append(artifact)

                tools_done.append(tool)
                el.append_log(job_id, exec_id, f" {tool} generated")

            except Exception as tool_err:
                logger.exception(f"Tool {tool} failed in job {job_name}")
                el.append_log(job_id, exec_id, f" {tool} failed: {tool_err}")

        # 5. Email delivery
        delivery   = job.get("delivery", {})
        recipients = [e.strip() for e in
                      (delivery.get("emails") or "").split(",") if e.strip()]

        if recipients and artifacts:
            el.append_log(job_id, exec_id, f"Sending email to {recipients}…")
            subject   = delivery.get("subject") or f"[Nexus AI] Job: {job_name}"
            body_html = f"""
<h2 style="font-family:sans-serif;color:#2c3e50">Job: {job_name}</h2>
<p style="font-family:sans-serif">{delivery.get('message', '')}</p>
<p style="font-family:sans-serif">
  <strong>Executed:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}<br>
  <strong>Rows returned:</strong> {len(rows)}<br>
  <strong>Tools generated:</strong> {', '.join(tools_done)}
</p>
<p style="font-family:sans-serif">Reports attached as PDF files.</p>
"""
            try:
                _send_email(
                    recipients, subject, body_html,
                    [{"filename": a["filename"],
                      "data":     a["data"],
                      "is_base64":a["is_base64"]}
                     for a in artifacts],
                )
                el.append_log(job_id, exec_id, " Email sent")
            except Exception as mail_err:
                logger.exception("Email delivery failed")
                el.append_log(job_id, exec_id, f" Email failed: {mail_err}")

        # 6. Write complete log entry
        el.finish_execution(
            job_id          = job_id,
            exec_id         = exec_id,
            status          = "success",
            tools_completed = tools_done,
            row_count       = len(rows),
            artifacts       = [{"tool": a["tool"], "filename": a["filename"]}
                                for a in artifacts],
            sql_query       = sql_q,
            columns         = columns,
            data            = rows,
            insights        = insights,
        )

        job_manager.update_run_meta(job_id, "success")
        if scheduler:
            _refresh_next_run(scheduler, job_id)

        logger.info(
            f" Job '{job_name}' complete — rows={len(rows)} "
            f"tools={tools_done} "
            f"creator={user_context.get('username')}"
        )

    except Exception as e:
        logger.exception(f" Job '{job_name}' failed")
        el.finish_execution(job_id, exec_id, "failed", error=str(e))
        job_manager.update_run_meta(job_id, "failed")
        if scheduler:
            _refresh_next_run(scheduler, job_id)


# ── Scheduler service ──────────────────────────────────────────────────────────

class SchedulerService:
    """Owns the process-wide APScheduler instance and bridges it to `job_manager`.

    Wraps a `BackgroundScheduler` (UTC timezone, 4-worker thread pool,
    ``coalesce=True``/``max_instances=1`` so a missed or slow run never stacks
    up duplicate executions) and exposes lifecycle hooks (`on_job_created`,
    `on_job_updated`, `on_job_deleted`, `on_job_toggled`) that the
    `blueprints.app_jobs_routes` CRUD endpoints call after each DB write, plus
    `run_now` for manual triggering. The module-level singleton
    `scheduler_service` is the intended entry point.
    """

    def __init__(self):
        from apscheduler.executors.pool import ThreadPoolExecutor as _APThreadPool
        self._scheduler = BackgroundScheduler(
            timezone  = "UTC",
            executors = {"default": _APThreadPool(max_workers=4)},
            job_defaults = {"coalesce": True, "max_instances": 1},
        )
        self._lock    = threading.Lock()
        self._started = False

    def start(self) -> None:
        """Start the underlying APScheduler (once) and load all enabled jobs into it.

        Safe to call multiple times — a no-op if already started.
        """
        with self._lock:
            if self._started:
                return
            self._scheduler.start()
            self._started = True
        logger.info(" APScheduler started")
        self._reload_all_jobs()

    def stop(self) -> None:
        """Shut down the APScheduler without waiting for in-flight jobs to finish."""
        with self._lock:
            if self._started:
                self._scheduler.shutdown(wait=False)
                self._started = False

    def _reload_all_jobs(self) -> None:
        """Load every enabled job from `job_manager` and register it with APScheduler.

        Called once from `start()` to repopulate the in-memory scheduler after
        a process restart (job definitions live in SQL Server, not in
        APScheduler's own state).
        """
        jobs = job_manager.load_jobs()
        for job in jobs:
            if job.get("enabled"):
                self._add_to_scheduler(job)
        logger.info(
            f" {sum(1 for j in jobs if j.get('enabled'))} jobs loaded on startup"
        )

    def _add_to_scheduler(self, job: Dict) -> None:
        """Build a trigger for ``job`` and (re-)register it with APScheduler.

        Removes any existing APScheduler entry for the job id first, so this
        is safe to call both for new jobs and to apply edits to an existing
        schedule. On success, refreshes the persisted ``next_run`` via
        `_refresh_next_run`.

        Args:
            job: Job dict; no-ops if ``job["schedule"]`` is empty/falsy.
        """
        jid      = job["id"]
        schedule = job.get("schedule", {})
        if not schedule:
            return
        if self._scheduler.get_job(jid):
            self._scheduler.remove_job(jid)
        try:
            trigger   = _build_trigger(schedule)
            sched_ref = self._scheduler

            def _run(j=job, s=sched_ref):
                _execute_job(j, scheduler=s)

            self._scheduler.add_job(
                func             = _run,
                trigger          = trigger,
                id               = jid,
                name             = job["name"],
                replace_existing = True,
                misfire_grace_time = 300,
                max_instances    = 1,
            )
            _refresh_next_run(self._scheduler, jid)
            logger.info(f" Scheduled: {job['name']}")
        except Exception as e:
            logger.error(f" Schedule failed for {jid}: {e}")

    def _remove_from_scheduler(self, job_id: str) -> None:
        """Unregister a job from APScheduler, if present. Does not touch the DB."""
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    def on_job_created(self, job: Dict) -> None:
        """Hook for `blueprints.app_jobs_routes.api_create_job`: schedule if enabled."""
        if job.get("enabled"):
            self._add_to_scheduler(job)

    def on_job_updated(self, job: Dict) -> None:
        """Hook for `blueprints.app_jobs_routes.api_update_job`: reschedule or remove."""
        if job.get("enabled"):
            self._add_to_scheduler(job)
        else:
            self._remove_from_scheduler(job["id"])

    def on_job_deleted(self, job_id: str) -> None:
        """Hook for `blueprints.app_jobs_routes.api_delete_job`: unregister the trigger."""
        self._remove_from_scheduler(job_id)

    def on_job_toggled(self, job_id: str, enabled: bool) -> None:
        """Hook for `blueprints.app_jobs_routes.api_toggle_job`: (re)schedule or remove.

        Args:
            job_id: Job UUID that was toggled.
            enabled: New enabled state (already persisted by `job_manager.toggle_job`).
        """
        if enabled:
            job = job_manager.get_job(job_id)
            if job:
                self._add_to_scheduler(job)
        else:
            self._remove_from_scheduler(job_id)

    def run_now(self, job_id: str) -> tuple:
        """Trigger an immediate, one-off run of ``job_id`` on a background daemon thread.

        Does not go through APScheduler — runs `_execute_job` directly so it
        executes immediately regardless of the job's configured schedule.

        Args:
            job_id: Job UUID to run.

        Returns:
            ``(True, "Job triggered")`` once the thread has been started, or
            ``(False, "Job not found")`` if the job doesn't exist. Note this
            reflects whether the run was *started*, not whether it succeeded
            — completion status is recorded asynchronously via
            `execution_logger`.
        """
        job = job_manager.get_job(job_id)
        if not job:
            return False, "Job not found"
        sched_ref = self._scheduler
        threading.Thread(
            target = _execute_job,
            args   = [job],
            kwargs = {"scheduler": sched_ref},
            daemon = True,
        ).start()
        return True, "Job triggered"

    def get_next_run(self, job_id: str) -> Optional[str]:
        """Return the job's next scheduled fire time as an ISO string.

        Prefers APScheduler's live in-memory value; falls back to the
        persisted ``app_jobs.next_run`` column if the job isn't currently
        registered in this process's scheduler.

        Args:
            job_id: Job UUID to look up.

        Returns:
            ISO timestamp string, or ``None`` if unknown / job not found.
        """
        apjob = self._scheduler.get_job(job_id)
        next_run_time = getattr(apjob, "next_run_time", None) if apjob else None
        if next_run_time:
            return next_run_time.isoformat()
        job = job_manager.get_job(job_id)
        return job.get("next_run") if job else None

    def is_running(self, job_id: str) -> bool:
        """Best-effort check of whether a job's most recent execution is still in flight.

        Looks at the single most recent execution log entry: if its status is
        ``"running"`` and it started less than 30 minutes ago, the job is
        considered running (the 30-minute cutoff guards against a crashed
        process leaving a stale "running" row forever).

        Args:
            job_id: Job UUID to check.

        Returns:
            True if the latest execution looks like it's still active.
        """
        logs = el.get_logs(job_id, limit=1)
        if not logs:
            return False
        entry = logs[0]
        if entry.get("status") != "running":
            return False
        try:
            started = datetime.fromisoformat(entry["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - started).total_seconds() / 60 < 30
        except Exception:
            return False


scheduler_service = SchedulerService()