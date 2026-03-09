"""blueprints/app_bi_generation_routes.py — dashboard/report/infographic/presentation generation + previews + email routes

Relocated from app.py verbatim (Phase 3 Slice 2 route reorganization).
Route functions are nested inside register_bi_generation_routes(...) exactly
like blueprints/app_jobs_routes.py::register_jobs_routes does, so they
register directly onto the real `app` object with their EXACT original
endpoint names and URL paths (no blueprint prefix) — existing url_for()
calls and hardcoded fetch() URLs throughout the app keep working unchanged.
"""
from flask import render_template, jsonify, request, redirect, url_for, Response, make_response, g
import json
import json as _json
import math
import time
import pandas as pd
from datetime import datetime, date
import config
import auth
import token_limits
import org_db
import nexus_sync_db
from logging_config import get_logger
from app_route_helpers import clean_nan
import bi_training_db as _bi_td
import bi_conversations_db
from clients_db import load_clients as _load_clients_db
from dashboard_generator import generate_dashboard_config, DASHBOARD_MODEL
from reportgenerator import generate_report_config, get_report_data_with_filters, _MODEL as REPORT_MODEL
from infographicgenerator import InfographicGenerator
from email_intelligence import (
    get_senders_for_client, get_emails,
    parse_date_range_from_question, format_emails_for_llm,
    resolve_client_db_cfg,
)

logger = get_logger(__name__)


def register_bi_generation_routes(app, db_manager, _ppt_gen):
    """Register BI dashboard/report/infographic/presentation generation routes onto app.

    Args:
        app: The Flask application instance.
    """

    @app.route('/api/bi-agents/generate-dashboard', methods=['POST'])
    @auth.login_required
    def generate_dashboard():
        """Generate a dashboard layout/config (LLM-assisted) from tabular rows, recording token usage and BI training data."""
        try:
            data          = request.get_json()
            rows          = data.get('rows')
            conversation_id = data.get('conversation_id')
            cache_key        = data.get('cache_key')

            if not rows:
                return jsonify({"status": "error", "message": "rows is required"}), 400

            df = pd.DataFrame(rows)
            df = df.where(pd.notnull(df), None)

            # generate_dashboard_config returns (config, tokens_used, input_tokens, output_tokens)
            config_out, tokens_used, _dash_in, _dash_out = generate_dashboard_config(df)

            if config_out is None:
                return jsonify({"status": "error", "message": "Failed to generate dashboard JSON"}), 400

            # ── Record tokens against the current user ────────────────────────
            user = auth.current_user()
            if user and user.get("id"):
                token_limits.record_usage(
                    user_id       = user["id"],
                    tokens        = tokens_used,
                    call_type     = "dashboard",
                    agent_name    = "",
                    question      = f"generate dashboard: {len(rows)} rows, {len(df.columns)} cols",
                    input_tokens  = _dash_in,
                    output_tokens = _dash_out,
                    model         = DASHBOARD_MODEL,
                )
                logger.info(
                    f"[token] dashboard_gen user={user.get('username')} "
                    f"tokens={tokens_used} rows={len(rows)}"
                )
            # ─────────────────────────────────────────────────────────────────

            raw_data    = df.to_dict('records')
            final_payload = {
                "type":    "dashboardData",
                "config":  clean_nan(config_out),
                "rawData": clean_nan(raw_data),
            }

            # ── BI TRAINING DATA ──────────────────────────────────────────────────
            _bi_td.save_async(
                tool_type   = "bi_dashboard",
                user        = user,
                instruction = f"Generate dashboard from {len(rows)} rows × {len(df.columns)} columns: {', '.join(str(c) for c in df.columns[:20])}",
                context     = json.dumps({"columns": list(df.columns), "row_count": len(rows)}),
                output      = json.dumps(clean_nan(config_out), ensure_ascii=False),
                agent_name  = "",
                model_used  = DASHBOARD_MODEL,
                token_count = tokens_used,
                domain      = "bi_dashboard",
                tags        = "dashboard",
            )
            # ─────────────────────────────────────────────────────────────────────

            # ── Persist so reopening this conversation restores it — see modals.html ──
            if conversation_id and cache_key:
                bi_conversations_db.save_artifact(conversation_id, "dashboard", cache_key, final_payload)

            return Response(json.dumps(final_payload, allow_nan=False), mimetype='application/json')

        except Exception as e:
            logger.exception("BI Agents Generate Dashboard Failed")
            return jsonify({"status": "error", "message": "Internal server error"}), 500


    @app.route('/api/bi-agents/dashboard-token-usage', methods=['GET'])
    @auth.login_required
    def dashboard_token_usage():
        """
        Returns today's dashboard-generation token usage for the current user.
        Useful for showing a lightweight token counter in the UI without
        fetching the full /api/auth/token-usage summary.
        """
        user = auth.current_user()
        try:
            with auth._get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT ISNULL(SUM(tokens_used), 0)
                    FROM   token_usage
                    WHERE  user_id  = ?
                      AND  call_type = 'dashboard'
                      AND  used_at  >= CAST(GETUTCDATE() AS DATE)
                """, user["id"])
                row = cursor.fetchone()
                used = int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"dashboard_token_usage query failed: {e}")
            used = 0

        limit = token_limits.DAILY_LIMITS.get(user.get("role"), 10_000)
        return jsonify({
            "unlimited":  False,
            "used_today": used,
            "limit":      limit,
            "remaining":  max(0, limit - used),
            "call_type":  "dashboard",
        })


    @app.route('/api/bi-agents/generate-report', methods=['POST'])
    @auth.login_required
    def generate_report():
        """Generate a report config (LLM-assisted) from tabular rows, recording token usage and BI training data."""
        try:
            data          = request.get_json()
            rows          = data.get('rows')
            conversation_id = data.get('conversation_id')
            cache_key        = data.get('cache_key')

            if not rows:
                return jsonify({"status": "error", "message": "rows is required"}), 400

            df = pd.DataFrame(rows)
            df = df.where(pd.notnull(df), None)

            # generate_report_config returns (config, tokens_used, input_tokens, output_tokens)
            report_config, tokens_used, _rep_in, _rep_out = generate_report_config(df)

            if report_config is None:
                return jsonify({"status": "error", "message": "Failed to generate report config"}), 400

            # ── Record tokens against the current user ────────────────────────
            user = auth.current_user()
            if user and user.get("id"):
                token_limits.record_usage(
                    user_id       = user["id"],
                    tokens        = tokens_used,
                    call_type     = "report",
                    agent_name    = "",
                    question      = f"generate report: {len(rows)} rows, {len(df.columns)} cols",
                    input_tokens  = _rep_in,
                    output_tokens = _rep_out,
                    model         = REPORT_MODEL,
                )
                logger.info(
                    f"[token] report_gen user={user.get('username')} "
                    f"tokens={tokens_used} rows={len(rows)}"
                )
            # ─────────────────────────────────────────────────────────────────

            raw_data = df.to_dict('records')

            # ── BI TRAINING DATA ──────────────────────────────────────────────────
            _bi_td.save_async(
                tool_type   = "bi_report",
                user        = user,
                instruction = f"Generate report from {len(rows)} rows × {len(df.columns)} columns: {', '.join(str(c) for c in df.columns[:20])}",
                context     = json.dumps({"columns": list(df.columns), "row_count": len(rows)}),
                output      = json.dumps(clean_nan(report_config), ensure_ascii=False),
                agent_name  = "",
                model_used  = REPORT_MODEL,
                token_count = tokens_used,
                domain      = "bi_report",
                tags        = "report",
            )
            # ─────────────────────────────────────────────────────────────────────

            final_payload = {
                "success":       True,
                "report_config": clean_nan(report_config),
                "raw_data":      clean_nan(raw_data),
            }

            # ── Persist so reopening this conversation restores it — see modals.html ──
            if conversation_id and cache_key:
                bi_conversations_db.save_artifact(conversation_id, "report", cache_key, final_payload)

            return jsonify(final_payload)

        except Exception as e:
            logger.exception("BI Agents Generate Report Failed")
            return jsonify({"success": False, "message": "Internal server error"}), 500


    @app.route('/api/bi-agents/generate-infographic', methods=['POST'])
    @auth.login_required
    def generate_infographic():
        """Generate an infographic layout (LLM-assisted) from summary bullet points + tabular rows."""
        try:
            data       = request.get_json()
            summary    = data.get('summary')
            rows       = data.get('rows')
            agent_name = data.get('agent_name', '')   # optional — sent by frontend
            conversation_id = data.get('conversation_id')
            cache_key        = data.get('cache_key')

            if not summary or not rows:
                return jsonify({"status": "error", "message": "Missing summary or rows data"}), 400

            user = auth.current_user()                 # pass user for token tracking

            gen    = InfographicGenerator()
            layout = gen.generate_infographic_layout(
                summary_points = summary,
                rows           = rows,
                user           = user,
                agent_name     = agent_name,
            )
            if layout is None:
                return jsonify({"status": "error", "message": "Infographic generation failed"}), 400

            # ── BI TRAINING DATA ──────────────────────────────────────────────────
            summary_text = "\n".join(f"• {p}" for p in (summary or []))
            _bi_td.save_async(
                tool_type   = "bi_infographic",
                user        = user,
                instruction = summary_text[:2000] or "Generate infographic",
                context     = json.dumps({
                    "agent_name": agent_name,
                    "row_count":  len(rows) if rows else 0,
                    "columns":    list((rows[0] or {}).keys())[:20] if rows else [],
                }),
                output      = json.dumps(layout, ensure_ascii=False),
                agent_name  = agent_name,
                model_used  = InfographicGenerator.MODEL,
                domain      = "bi_infographic",
                tags        = f"infographic,{agent_name}" if agent_name else "infographic",
            )
            # ─────────────────────────────────────────────────────────────────────

            # ── Persist so reopening this conversation restores it — see modals.html ──
            if conversation_id and cache_key:
                bi_conversations_db.save_artifact(
                    conversation_id, "infographic", cache_key,
                    {"status": "success", "infographic": layout},
                )

            # Attach current token usage so the frontend can show the budget bar
            token_summary = token_limits.get_usage_summary(user)
            return jsonify({
                "status":      "success",
                "infographic": layout,
                "token_usage": token_summary,
            })
        except Exception as e:
            logger.exception("BI Agents Generate Infographic Failed")
            return jsonify({"status": "error", "message": "Internal server error"}), 500


    @app.route('/preview/dashboard')
    @auth.login_required
    def preview_dashboard():
        """Render the standalone dashboard preview/layout page."""
        return render_template('dashboardlayout.html')


    @app.route('/preview/report')
    @auth.login_required
    def preview_report():
        """Render the standalone report preview/layout page."""
        return render_template('reportlayout.html')


    @app.route('/preview/infographic')
    @auth.login_required
    def preview_infographic():
        """Render the standalone infographic preview/layout page."""
        return render_template('infographiclayout.html')


    @app.route('/preview/presentation')
    @auth.login_required
    def preview_presentation():
        """Render the standalone presentation preview/layout page."""
        return render_template('presentationlayout.html')


    @app.route('/api/bi-agents/email-senders', methods=['POST'])
    def email_senders():
        """Look up the distinct senders who emailed about a given client (for the PPT/email-intelligence flow).

        Note: unlike the sibling email_fetch() route below, this endpoint has no
        @auth.login_required decorator.

        POST body: { "client_name": "AAI" }
        Returns:   { "status": "success", "senders": ["Kasim Sah", ...] }
                   or { "status": "no_emails", "senders": [] }
        """
        try:
            data        = request.get_json(force=True) or {}
            client_name = data.get('client_name', '').strip()
            client_id   = data.get('client_id', '').strip()
            if not client_name:
                return jsonify({"status": "error", "message": "client_name required"}), 400

            # Auto-resolve client_id from name if not supplied by the caller
            if not client_id:
                for cid, c in _load_clients_db().items():
                    if c.get('name', '').lower() == client_name.lower():
                        client_id = cid
                        break

            db_cfg = resolve_client_db_cfg(
                (_load_clients_db().get(client_id) or {}).get('email_db_config', {}),
                db_manager,
            ) if client_id else {}

            senders = get_senders_for_client(client_name, db_cfg=db_cfg or None, client_id=client_id)
            if not senders:
                return jsonify({"status": "no_emails", "senders": []})
            return jsonify({"status": "success", "senders": senders})

        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"status": "no_emails", "senders": [],
                            "detail": str(exc)})


    @app.route('/api/bi-agents/email-fetch', methods=['POST'])
    @auth.login_required
    def email_fetch():
        """
        POST body:
        {
          "client_name":   "AAI",
          "sender_names":  ["Kasim Sah", "Ankit Nigam"],
          "question":      "show me AAI updates this week"   ← used for date parsing
        }
        Returns: { "status": "success", "email_text": "EMAIL INTELLIGENCE ..." }
        """
        try:
            data         = request.get_json(force=True) or {}
            client_name  = data.get('client_name', '').strip()
            client_id    = data.get('client_id', '').strip()
            sender_names = data.get('sender_names') or []
            question     = data.get('question', '')

            if not client_name or not sender_names:
                return jsonify({"status": "error",
                                "message": "client_name and sender_names required"}), 400

            # Auto-resolve client_id from name if not supplied by the caller
            if not client_id:
                for cid, c in _load_clients_db().items():
                    if c.get('name', '').lower() == client_name.lower():
                        client_id = cid
                        break

            db_cfg = resolve_client_db_cfg(
                (_load_clients_db().get(client_id) or {}).get('email_db_config', {}),
                db_manager,
            ) if client_id else {}

            date_from, date_to = parse_date_range_from_question(question)
            emails      = get_emails(client_name, sender_names, date_from, date_to,
                                     db_cfg=db_cfg or None, client_id=client_id)
            email_text  = format_emails_for_llm(emails)

            return jsonify({
                "status":      "success",
                "email_count": len(emails),
                "email_text":  email_text,
                "date_from":   date_from,
                "date_to":     date_to,
            })

        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"status": "no_emails", "email_count": 0,
                            "email_text": "", "detail": str(exc)})


    @app.route('/api/bi-agents/generate-presentation', methods=['POST'])
    @auth.login_required
    def generate_presentation():
        """
        POST body:
        {
          "summary":      ["bullet 1", ...],
          "rows":         [{...}, ...],
          "client_id":    "aai",        ← optional; auto-detected if omitted
          "question":     "show me AAI delivery status",
          "email_text":   "EMAIL INTELLIGENCE ..."   ← optional; from /email-fetch
        }
        """
        try:
            current_user = getattr(g, 'current_user', None)
            if current_user:
                try:
                    import token_limits as _tl
                    allowed, msg, _, _ = _tl.check_limit(current_user)
                    if not allowed:
                        return jsonify({"status": "error", "message": msg}), 429
                except ImportError:
                    pass

            data       = request.get_json(force=True) or {}
            summary    = data.get('summary') or []
            rows       = data.get('rows') or []
            client_id  = data.get('client_id', '')
            question   = data.get('question', '')
            email_text = data.get('email_text', '')     # ← new

            if not rows:
                return jsonify({"status": "error", "message": "rows is required"}), 400

            client_cfg = _load_clients_db().get(client_id, {}) if client_id else {}

            result = _ppt_gen.generate(
                summary_points=summary,
                rows=rows,
                client_config=client_cfg,
                question=question,
                user=current_user,
                email_text=email_text,                  # ← new
            )

            # ── BI TRAINING DATA ──────────────────────────────────────────────────
            if result.get("status") == "success":
                client_name = (result.get("client") or {}).get("name", client_id)
                summary_text = "\n".join(f"• {p}" for p in (summary or []))
                _bi_td.save_async(
                    tool_type   = "bi_presentation",
                    user        = current_user,
                    instruction = question or summary_text[:500] or "Generate presentation",
                    context     = _json.dumps({
                        "client":       client_name,
                        "summary":      summary_text[:1000],
                        "email_text":   (email_text or "")[:500],
                        "row_count":    len(rows),
                    }, ensure_ascii=False),
                    output      = _json.dumps(result.get("slides", []), ensure_ascii=False),
                    agent_name  = client_name,
                    model_used  = "gpt-4o",
                    token_count = result.get("tokens_used"),
                    domain      = "bi_presentation",
                    tags        = f"presentation,{client_name}",
                )
            # ─────────────────────────────────────────────────────────────────────

            return Response(
                _json.dumps(result, allow_nan=False, default=str),
                mimetype='application/json'
            )

        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"status": "error", "message": str(exc)}), 500


    @app.route('/api/bi-agents/optimize-presentation', methods=['POST'])
    @auth.login_required
    def optimize_presentation():
        """
        POST body:
        {
          "slides":      [...],
          "instruction": "make slide 2 focus more on ROI",
          "client_id":   "aai"
        }
        """
        try:
            current_user = getattr(g, 'current_user', None)
            if current_user:
                try:
                    import token_limits as _tl
                    allowed, msg, _, _ = _tl.check_limit(current_user)
                    if not allowed:
                        return jsonify({"status": "error", "message": msg}), 429
                except ImportError:
                    pass

            data         = request.get_json(force=True) or {}
            slides       = data.get('slides') or []
            instruction  = data.get('instruction', '').strip()
            client_id    = data.get('client_id', '')
            data_summary = data.get('data_summary') or None

            if not slides:
                return jsonify({"status": "error", "message": "slides is required"}), 400
            if not instruction:
                return jsonify({"status": "error", "message": "instruction is required"}), 400

            client_cfg = _load_clients_db().get(client_id, {}) if client_id else {}

            result = _ppt_gen.optimize(
                current_slides=slides,
                instruction=instruction,
                client_config=client_cfg,
                user=current_user,
                data_summary=data_summary,
            )

            # ── BI TRAINING DATA ──────────────────────────────────────────────────
            if result.get("status") == "success":
                client_name = (result.get("client") or {}).get("name", client_id)
                _bi_td.save_async(
                    tool_type   = "bi_presentation_optimize",
                    user        = current_user,
                    instruction = instruction,
                    context     = _json.dumps({
                        "client":          client_name,
                        "original_slides": _json.dumps(slides, ensure_ascii=False)[:2000],
                    }, ensure_ascii=False),
                    output      = _json.dumps(result.get("slides", []), ensure_ascii=False),
                    agent_name  = client_name,
                    model_used  = "gpt-4o",
                    token_count = result.get("tokens_used"),
                    domain      = "bi_presentation",
                    tags        = f"presentation,optimize,{client_name}",
                )
            # ─────────────────────────────────────────────────────────────────────

            return Response(
                _json.dumps(result, allow_nan=False, default=str),
                mimetype='application/json'
            )

        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"status": "error", "message": str(exc)}), 500

