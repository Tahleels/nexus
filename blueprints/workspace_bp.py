"""
workspace_bp.py — Flask Blueprint: Enterprise AI Workspace.

Register in app.py:
    from workspace_bp import workspace_bp
    app.register_blueprint(workspace_bp)

All routes require @auth.login_required, and the blueprint's
before_request hook (_require_rnd_department) further restricts access
to admins/devs or users in the "R&D" department.

Routes
------
  GET  /workspace                               — main chat page
  GET  /workspace/projects                       — projects page
  GET  /workspace/saved-chats                    — starred + recent conversations page
  GET  /workspace/prompt-library                 — prompt library page
  GET  /workspace/artifacts                      — artifacts page
  GET  /workspace/team                           — team workspaces page
  GET  /workspace/memory                         — enterprise memory page
  GET  /workspace/activity                       — activity log page
  GET  /workspace/analytics                      — analytics dashboard page (also carries the
                                                     per-model usage/cost breakdown)
  GET  /workspace/settings                       — user settings page

  POST /api/workspace/chat/stream                — SSE streaming chat (main endpoint)

  GET  /api/workspace/conversations              — list conversations
  POST /api/workspace/conversations              — create conversation
  GET  /api/workspace/conversations/<id>         — get conversation + messages + citations
  PATCH /api/workspace/conversations/<id>        — update conversation
  DELETE /api/workspace/conversations/<id>       — delete (archive) conversation

  GET  /api/workspace/users/search               — search users (for adding to a workspace)
  GET  /api/workspace/workspaces                 — list workspaces
  POST /api/workspace/workspaces                 — create workspace
  PATCH /api/workspace/workspaces/<id>           — update workspace
  DELETE /api/workspace/workspaces/<id>          — delete (archive) workspace
  GET  /api/workspace/workspaces/<id>/members            — list members
  POST /api/workspace/workspaces/<id>/members            — add member
  PATCH /api/workspace/workspaces/<id>/members/<uid>     — update member role
  DELETE /api/workspace/workspaces/<id>/members/<uid>    — remove member

  GET  /api/workspace/projects                   — list projects
  POST /api/workspace/projects                   — create project
  PUT  /api/workspace/projects/<id>               — update project (owner/admin/dev only)
  DELETE /api/workspace/projects/<id>             — delete project (owner/admin/dev only)
  GET  /api/workspace/projects/<id>/files               — list project RAG files
  POST /api/workspace/projects/<id>/files               — upload + embed a project RAG file
  DELETE /api/workspace/projects/<id>/files/<file_id>    — delete a project RAG file

  POST /api/workspace/extract-file               — extract text from an uploaded file (no indexing)

  GET  /api/workspace/artifacts                  — list artifacts
  GET  /api/workspace/artifacts/<id>              — get a single artifact
  POST /api/workspace/artifacts                  — save an artifact
  DELETE /api/workspace/artifacts/<id>            — delete an artifact
  PATCH /api/workspace/artifacts/<id>             — update an artifact's title/content
  POST /api/workspace/artifacts/<id>/star         — toggle artifact star

  GET  /api/workspace/prompts                    — list prompt library entries
  POST /api/workspace/prompts                    — create prompt library entry
  POST /api/workspace/prompts/<id>/use            — increment use count
  DELETE /api/workspace/prompts/<id>              — delete prompt library entry

  GET  /api/workspace/memory                     — list enterprise memory entries
  POST /api/workspace/memory                     — save enterprise memory entry
  DELETE /api/workspace/memory/<id>               — delete (deactivate) memory entry

  POST /api/workspace/messages/<id>/feedback      — thumbs_up / thumbs_down
  POST /api/workspace/messages/<id>/copy          — log copy event
  PATCH /api/workspace/messages/<id>/edit         — edit message + save history

  GET  /api/workspace/analytics/summary           — analytics summary

  GET  /api/workspace/settings                    — get user settings
  POST /api/workspace/settings                    — save user settings

  GET  /api/workspace/activity                   — activity log (all users if admin)
"""

import io
import json
from logging_config import get_logger
import os
import threading
from flask import (
    Blueprint, render_template, jsonify, request,
    Response, stream_with_context, g, redirect, url_for,
)

import auth
import token_limits
import workspace_db
import workspace_openai_service as openai_svc
from core.teams_crypto import teams_encrypt, teams_decrypt

logger = get_logger(__name__)

workspace_bp = Blueprint(
    "workspace",
    __name__,
    template_folder="templates",
    url_prefix="",
)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _user():
    """Return the currently authenticated user dict (shortcut for ``auth.current_user()``)."""
    return auth.current_user()


def _ip():
    """Return the requesting client's IP address (or "" if unavailable), for activity logging."""
    return request.remote_addr or ""


@workspace_bp.before_request
def _require_rnd_department():
    """Workspace is scoped to the R&D department — admins/devs always pass.

    Runs before every request to this blueprint. Non-admin/dev users must
    belong to the "R&D" department; otherwise API requests get a 403 JSON
    response and page requests are redirected to the main dashboard.
    """
    user = auth.current_user()
    if not user or user.get("role") in ("admin", "dev"):
        return
    if not auth.user_in_department(user["id"], "R&D"):
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "You don't have access to the R&D workspace"}), 403
        return redirect(url_for("dashboard"))


# ─────────────────────────────────────────────────────────────
# PAGE ROUTES
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/workspace")
@auth.login_required
def workspace_chat():
    """Render the main AI Workspace chat page.

    Reads optional ``workspace``/``project`` query params to scope the
    conversation/project lists, restricts the offered model list to
    "gpt-4o" only for non-admin/dev users, and passes team workspaces for
    the workspace switcher.
    """
    user = _user()
    workspace_id = request.args.get("workspace", type=int)
    project_id   = request.args.get("project",   type=int)
    workspace = None
    if workspace_id:
        if not workspace_db.is_workspace_member(workspace_id, user["id"]):
            workspace_id = None
        else:
            all_ws = workspace_db.get_workspaces(user["id"])
            workspace = next((w for w in all_ws if w["id"] == workspace_id), None)
    conversations = workspace_db.get_conversations(user["id"], workspace_id=workspace_id, project_id=project_id, limit=50)
    projects = workspace_db.get_projects(user["id"], workspace_id=workspace_id)
    active_project = workspace_db.get_project(project_id) if project_id else None
    settings = workspace_db.get_user_settings(user["id"])
    is_power = user.get("role") in ("admin", "dev")
    models = openai_svc.AVAILABLE_MODELS if is_power else [m for m in openai_svc.AVAILABLE_MODELS if m["id"] == "gpt-4o"]
    all_workspaces = workspace_db.get_workspaces(user["id"])
    team_workspaces = [w for w in all_workspaces if w.get("is_team")]
    return render_template(
        "workspace/chat.html",
        conversations=conversations,
        projects=projects,
        active_project=active_project,
        settings=settings,
        models=models,
        workspace=workspace,
        workspace_id=workspace_id or 0,
        project_id=project_id or 0,
        current_user_id=user["id"],
        team_workspaces=team_workspaces,
        user_role=user.get("role", "user"),
    )


@workspace_bp.route("/workspace/projects")
@auth.login_required
def workspace_projects():
    """Render the projects list page, optionally scoped to a ``?workspace=`` query param."""
    user         = _user()
    workspace_id = request.args.get("workspace", type=int)
    workspace    = None
    if workspace_id and workspace_db.is_workspace_member(workspace_id, user["id"]):
        all_ws    = workspace_db.get_workspaces(user["id"])
        workspace = next((w for w in all_ws if w["id"] == workspace_id), None)
    else:
        workspace_id = None
    is_admin   = user.get("role") in ("admin", "dev")
    projects   = workspace_db.get_projects(user["id"], workspace_id=workspace_id, is_admin=is_admin)
    workspaces = workspace_db.get_workspaces(user["id"])
    return render_template(
        "workspace/projects.html",
        projects=projects,
        workspaces=workspaces,
        workspace=workspace,
        workspace_id=workspace_id or 0,
        current_user_id=user["id"],
        user_role=user.get("role", "user"),
    )


@workspace_bp.route("/workspace/saved-chats")
@auth.login_required
def workspace_saved_chats():
    """Render the saved chats page: all starred conversations (team-wide) plus the user's recent unstarred ones."""
    user   = _user()
    starred = workspace_db.get_all_starred_conversations(limit=300)
    recent  = workspace_db.get_conversations(user["id"], limit=100)
    recent  = [c for c in recent if not c.get("is_starred")]
    return render_template(
        "workspace/saved_chats.html",
        starred=starred,
        recent=recent,
    )


@workspace_bp.route("/workspace/prompt-library")
@auth.login_required
def workspace_prompt_library():
    """Render the prompt library page with all prompts and their distinct categories."""
    user = _user()
    prompts = workspace_db.get_prompts(user["id"], include_shared=True)
    categories = sorted({p.get("category", "general") for p in prompts})
    return render_template(
        "workspace/prompt_library.html",
        prompts=prompts,
        categories=categories,
    )


@workspace_bp.route("/workspace/artifacts")
@auth.login_required
def workspace_artifacts():
    """Render the artifacts gallery page (up to 100 most recent artifacts)."""
    user = _user()
    artifacts = workspace_db.get_artifacts(user["id"], limit=100)
    return render_template("workspace/artifacts.html", artifacts=artifacts)


@workspace_bp.route("/workspace/team")
@auth.login_required
def workspace_team():
    """Render the team workspaces page (only the user's team-flagged workspaces)."""
    user = _user()
    workspaces = workspace_db.get_workspaces(user["id"])
    team_ws    = [w for w in workspaces if w.get("is_team")]
    return render_template("workspace/team.html", workspaces=team_ws)


@workspace_bp.route("/workspace/memory")
@auth.login_required
def workspace_memory():
    """Render the enterprise memory page."""
    user      = _user()
    is_admin  = user.get("role") in ("admin", "dev")
    admin_all = is_admin  # admins see all memory entries
    memories  = workspace_db.get_memory(user["id"], admin_all=admin_all)
    rd_users  = workspace_db.get_all_rd_users() if is_admin else []
    workspaces = workspace_db.get_workspaces(user["id"]) if is_admin else []
    return render_template("workspace/memory.html", memories=memories,
                           is_admin=is_admin, rd_users=rd_users,
                           workspaces=workspaces)


@workspace_bp.route("/workspace/activity")
@auth.login_required
def workspace_activity():
    """Render the activity log page (the user's most recent 200 activity entries)."""
    user     = _user()
    activity = workspace_db.get_activity(user["id"], limit=200)
    return render_template("workspace/activity.html", activity=activity)


@workspace_bp.route("/workspace/analytics")
@auth.login_required
def workspace_analytics():
    """Render the analytics dashboard for the trailing N days (``?days=``, default 30).

    Also carries the per-model token/cost breakdown that used to live on its
    own "Model Usage" page (merged here — same date range, mostly the same
    underlying queries, so a separate page was pure duplication). Admins
    additionally see an all-users breakdown.
    """
    user      = _user()
    days      = int(request.args.get("days", 30))
    summary   = workspace_db.get_workspace_analytics(user["id"], days=days)
    daily     = workspace_db.get_daily_model_usage(user["id"], days=days)
    usage     = workspace_db.get_model_usage_summary(user["id"], days=days)
    all_usage = workspace_db.get_all_users_model_usage(days=days) if user["role"] == "admin" else []
    return render_template(
        "workspace/analytics.html",
        summary=summary,
        daily_usage=daily,
        usage=usage,
        all_usage=all_usage,
        days=days,
    )


@workspace_bp.route("/workspace/settings")
@auth.login_required
def workspace_settings():
    """Render the user settings page (model list restricted to "gpt-4o" for non-admin/dev users)."""
    user     = _user()
    settings = workspace_db.get_user_settings(user["id"])
    is_power = user.get("role") in ("admin", "dev")
    models   = openai_svc.AVAILABLE_MODELS if is_power else [m for m in openai_svc.AVAILABLE_MODELS if m["id"] == "gpt-4o"]
    return render_template(
        "workspace/settings.html",
        settings=settings,
        models=models,
    )


# ─────────────────────────────────────────────────────────────
# API — STREAMING CHAT  (Server-Sent Events)
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/chat/stream", methods=["POST"])
@auth.login_required
def api_chat_stream():
    """Stream a chat response via Server-Sent Events (the main Workspace chat endpoint).

    Request JSON body:
        conversation_id (int, optional): Existing conversation to continue;
            a new one is created if omitted.
        message (str, required): The user's message text.
        model (str): Model id; forced to "gpt-4o" for non-admin/dev users.
        tools (list[str]): Enabled tool names, e.g. ["web_search"], ["teams_chat"].
        system_prompt (str): Ad-hoc system prompt (combined with any project
            system prompt/notes/RAG context).
        project_id (int, optional): Project to pull system prompt/notes/RAG
            context from.
        workspace_id (int, optional): Team workspace to attach the (new)
            conversation to.
        images (list[dict], optional): ``[{name, mimeType, dataUrl}, ...]``
            file attachments for vision-capable models.

    Behaviour:
        1. Enforces the existing daily token budget (``token_limits.check_and_record``).
        2. Resolves project context (system prompt + notes + a RAG search
           over the project's embedded files) if ``project_id`` is given.
        3. Creates the conversation if this is the first message.
        4. Trims message history to fit a ~120K-token budget (reserving
           4K tokens for the completion), summarising any dropped older
           messages into a single compact note.
        5. Persists the user message, then streams the assistant response
           from ``workspace_openai_service.stream_workspace_chat`` straight
           to the browser as ``text/event-stream`` — no DB writes happen
           during streaming. Once the "done" event arrives, a background
           thread (``_flush_to_db``) batch-writes the message content,
           chunks, citations, conversation stats, token-budget usage,
           model usage, replay events, and (for new conversations) an
           auto-generated title.

    Returns:
        A Flask ``Response`` streaming ``text/event-stream`` SSE data, or a
        JSON error response (400 for empty message, 429 if over budget).
    """
    user = _user()
    data = request.get_json(silent=True) or {}

    conversation_id  = data.get("conversation_id")
    user_message     = (data.get("message") or "").strip()
    model            = data.get("model", "gpt-4o")
    provider         = data.get("provider", "openai")
    if user.get("role") not in ("admin", "dev"):
        model    = "gpt-4o"
        provider = "openai"
    tools_enabled    = data.get("tools", [])
    system_prompt    = data.get("system_prompt", "")
    project_id       = data.get("project_id")
    workspace_id     = data.get("workspace_id") or None
    images           = data.get("images", [])  # [{name, mimeType, dataUrl}]

    if not user_message:
        return jsonify({"status": "error", "message": "Empty message"}), 400

    # Token budget check (existing system)
    allowed, limit_msg = token_limits.check_and_record(user, user_message)
    if not allowed:
        return jsonify({"status": "error", "message": limit_msg}), 429

    # Resolve project context — system prompt + notes + RAG chunks injected once
    project = workspace_db.get_project(project_id) if project_id else None
    if project:
        parts = []
        if (project.get("system_prompt") or "").strip():
            parts.append(f"[Project: {project['name']}]\n\n{project['system_prompt'].strip()}")
        if (project.get("notes") or "").strip():
            parts.append(f"PROJECT NOTES:\n{project['notes'].strip()}")

        # RAG — retrieve relevant chunks from project files
        project_files = workspace_db.get_project_files(project_id) if project_id else []
        if project_files:
            try:
                from agents.core.knowledge.vector_store import get_vector_store
                doc_ids = {f["document_id"] for f in project_files}
                vs = get_vector_store()
                hits = vs.search_documents(user_message, n_results=5, doc_ids=doc_ids)
                if hits:
                    rag_block = "RELEVANT PROJECT KNOWLEDGE:\n" + "\n\n".join(
                        f"[{h['filename']}]\n{h['text']}" for h in hits
                    )
                    parts.append(rag_block)
            except Exception as _rag_err:
                logger.warning("project RAG failed: %s", _rag_err)

        if system_prompt and system_prompt.strip():
            parts.append(system_prompt.strip())
        effective_system_prompt = "\n\n---\n\n".join(parts) if parts else None
        effective_model    = project.get("model") or model
        effective_provider = project.get("provider") or provider
    else:
        effective_system_prompt = system_prompt or None
        effective_model    = model
        effective_provider = provider

    # Inject enterprise memory into system prompt (personal + workspace + enterprise scopes)
    try:
        memory_entries = workspace_db.get_memory(user["id"], workspace_id=workspace_id)
        if memory_entries:
            mem_block = "PERSISTENT MEMORY (facts you must always remember):\n" + "\n".join(
                f"• {m['memory_key']}: {m['memory_value']}" for m in memory_entries
            )
            effective_system_prompt = (
                f"{mem_block}\n\n---\n\n{effective_system_prompt}"
                if effective_system_prompt else mem_block
            )
    except Exception as _mem_err:
        logger.warning("memory injection failed: %s", _mem_err)

    # Build project-specific extra tool defs (admin-configured tools beyond defaults)
    extra_tool_defs = []
    if project and project.get("tools_enabled"):
        try:
            import json as _j
            project_tool_names = _j.loads(project["tools_enabled"] or "[]")
            for tname in project_tool_names:
                td = openai_svc._build_registry_tool_def(tname)
                if td:
                    extra_tool_defs.append(td)
        except Exception as _td_err:
            logger.warning("project tool defs failed: %s", _td_err)

    # Create conversation on first message
    if not conversation_id:
        conversation_id = workspace_db.create_conversation(
            user_id=user["id"],
            model=effective_model,
            system_prompt=effective_system_prompt or "",
            project_id=project_id,
            workspace_id=workspace_id,
            tools_enabled=tools_enabled,
        )

    # Build message history using token-budget trimming, not a fixed count.
    # GPT-4o has a 128K context window. We reserve space for the system prompt,
    # the new user message, and the completion, then fit as many recent
    # messages as possible — exactly how ChatGPT/Claude manage history.
    _CONTEXT_TOKENS   = 120_000   # safe headroom under 128K
    _CHARS_PER_TOKEN  = 4         # ~4 chars ≈ 1 token (rough but fast)
    _COMPLETION_RESERVE = 4_000   # tokens reserved for the AI reply

    budget = _CONTEXT_TOKENS - _COMPLETION_RESERVE
    if effective_system_prompt:
        budget -= len(effective_system_prompt) // _CHARS_PER_TOKEN
    budget -= len(user_message) // _CHARS_PER_TOKEN

    raw_history = workspace_db.get_conversation_messages(conversation_id, limit=500)
    raw_history = [m for m in raw_history
                   if m["role"] in ("user", "assistant") and m.get("content")]

    # Walk newest → oldest, include messages that fit within budget
    selected = []
    dropped = []
    for m in reversed(raw_history):
        plain = teams_decrypt(m["content"])   # no-op if not encrypted
        cost = max(1, len(plain) // _CHARS_PER_TOKEN)
        if budget - cost < 0:
            dropped.append({**m, "content": plain})
        else:
            budget -= cost
            selected.append({"role": m["role"], "content": plain})

    selected.reverse()  # restore chronological order

    # If older messages were dropped, prepend a compact summary as a system note
    # so the model knows earlier context existed (same as ChatGPT's summarisation)
    if dropped:
        dropped_text = "\n".join(
            f"{'User' if m['role']=='user' else 'Assistant'}: {m['content'][:300]}{'…' if len(m['content'])>300 else ''}"
            for m in reversed(dropped)   # content already decrypted above
        )
        summary_note = (
            f"[Earlier in this conversation — {len(dropped)} message(s) summarised to save context]\n"
            f"{dropped_text}"
        )
        selected.insert(0, {"role": "user", "content": summary_note})
        selected.insert(1, {"role": "assistant", "content": "Understood, I have the earlier context noted."})

    selected.append({"role": "user", "content": user_message})
    messages = selected

    # Persist user message
    user_msg_id = workspace_db.save_message(
        conversation_id=conversation_id,
        user_id=user["id"],
        role="user",
        content=user_message,
        model=model,
    )
    workspace_db.log_activity(user["id"], "chat_message", "conversation",
                               conversation_id, {"model": model}, _ip())

    def _generate():
        """SSE generator: stream provider events to the browser, then fire a background DB flush.

        All chunk text is buffered in memory; NO DB writes happen during
        streaming. A single background thread (``_flush_to_db``) flushes
        everything to SQL Server after the stream is done. This eliminates
        hundreds of network round-trips that were blocking every character
        from appearing on screen.

        Yields:
            Raw ``"data: <json>\\n\\n"`` SSE-formatted strings.
        """
        full_text          = ""
        chunk_buffer: list = []   # [(index, text)] — flushed to DB after done
        tool_events: list  = []   # tool_start / tool_done events for replay
        prompt_tokens      = 0
        completion_tokens  = 0
        total_tokens       = 0
        latency_ms         = 0
        citations: list    = []
        teams_tool_called  = False   # set True when Teams tool fires → encrypt stored content

        # Create placeholder assistant message — ONE DB write before streaming
        asst_msg_id = workspace_db.save_message(
            conversation_id=conversation_id,
            user_id=user["id"],
            role="assistant",
            content="",
            model=model,
        )

        yield (
            f"data: {json.dumps({'type': 'start', 'conversation_id': conversation_id, 'message_id': asst_msg_id})}\n\n"
        )

        try:
            for event in openai_svc.stream_workspace_chat(
                messages=messages,
                model=effective_model,
                provider=effective_provider,
                tools_enabled=tools_enabled,
                system_prompt=effective_system_prompt,
                temperature=project.get("temperature", 0.7) if project else 0.7,
                images=images,
                user_email=user.get("email", ""),
                extra_tool_defs=extra_tool_defs,
            ):
                etype = event.get("type")

                if etype == "chunk":
                    chunk_text = event["text"]
                    full_text += chunk_text
                    chunk_buffer.append((len(chunk_buffer) + 1, chunk_text))
                    # ← ONLY send to browser; no DB write here
                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk_text})}\n\n"

                elif etype in ("tool_start", "tool_done"):
                    tool_events.append(event)
                    if event.get("tool") == "get_teams_chats_with_person":
                        teams_tool_called = True
                    yield f"data: {json.dumps(event)}\n\n"

                elif etype == "done":
                    full_text         = event.get("full_text", full_text)
                    prompt_tokens     = event.get("prompt_tokens", 0)
                    completion_tokens = event.get("completion_tokens", 0)
                    total_tokens      = event.get("total_tokens", 0)
                    latency_ms        = event.get("latency_ms", 0)
                    citations         = event.get("citations", [])
                    cost              = event.get("estimated_cost", 0.0)

                    # Tell the browser streaming is done immediately
                    yield f"data: {json.dumps({**event, 'message_id': asst_msg_id, 'conversation_id': conversation_id})}\n\n"

                    # ── Flush everything to DB in a background thread ──────
                    # This means the browser is already showing the full
                    # response while DB writes happen asynchronously.
                    def _flush_to_db(
                        _conv_id=conversation_id, _asst_id=asst_msg_id,
                        _user=user, _model=model, _full=full_text,
                        _pt=prompt_tokens, _ct=completion_tokens,
                        _tt=total_tokens, _lms=latency_ms, _cits=citations,
                        _cost=cost, _chunks=list(chunk_buffer),
                        _tools=list(tool_events), _umsg=user_message,
                        _teams=teams_tool_called,
                    ):
                        """Run in a daemon thread once streaming is done: persist the full turn to SQL Server.

                        Captures all needed values as default-argument snapshots
                        (rather than closing over the enclosing loop's mutable
                        variables) so the thread sees a consistent view even if
                        ``_generate`` has already returned. Writes, in order:
                        message content+stats, chunk batch, citations,
                        conversation counters, token-budget usage, model usage,
                        replay events (message + tool events), and an
                        auto-generated title for new conversations. All
                        exceptions are caught and logged — failures here never
                        surface to the user since the response was already sent.
                        """
                        try:
                            # Encrypt content if Teams tool was invoked this turn
                            _store = teams_encrypt(_full) if _teams else _full
                            # 1. Update assistant message content + stats
                            workspace_db.update_message(
                                _asst_id,
                                content=_store,
                                prompt_tokens=_pt,
                                completion_tokens=_ct,
                                total_tokens=_tt,
                                latency_ms=_lms,
                                finish_reason="stop",
                            )
                            # 2. Batch-insert all chunks
                            if _chunks:
                                workspace_db.batch_save_chunks(_asst_id, _chunks)
                            # 3. Save citations
                            for idx, cit in enumerate(_cits):
                                workspace_db.save_citation(_asst_id, cit, idx)
                            # 4. Update conversation counters
                            workspace_db.update_conversation_stats(_conv_id, _tt)
                            # 5. Token budget recording
                            token_limits.record_tokens(
                                user=_user,
                                call_type="workspace_chat",
                                question=_umsg,
                                actual_tokens=_tt if _tt else None,
                                response=_full,
                                input_tokens=_pt,
                                output_tokens=_ct,
                                model=_model,
                            )
                            # 6. Model-level usage
                            workspace_db.record_model_usage(
                                user_id=_user["id"],
                                model=_model,
                                conversation_id=_conv_id,
                                prompt_tokens=_pt,
                                completion_tokens=_ct,
                                cost=_cost,
                            )
                            # 7. Auto-title (also in this thread — no extra latency for user)
                            conv = workspace_db.get_conversation(_conv_id, _user["id"])
                            if conv and not conv.get("title"):
                                title = openai_svc.generate_conversation_title(_umsg)
                                workspace_db.update_conversation(_conv_id, {"title": title})
                        except Exception as bg_err:
                            logger.error(f"_flush_to_db background error: {bg_err}")

                    threading.Thread(target=_flush_to_db, daemon=True).start()
                    return

                elif etype == "error":
                    yield f"data: {json.dumps(event)}\n\n"
                    return

        except Exception as exc:
            logger.exception("SSE generate() failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────────────────────
# API — CONVERSATIONS
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/conversations", methods=["GET"])
@auth.login_required
def api_list_conversations():
    """List the user's conversations, optionally filtered by ``project_id``/``workspace_id`` query params."""
    user = _user()
    project_id   = request.args.get("project_id", type=int)
    workspace_id = request.args.get("workspace_id", type=int)
    convs = workspace_db.get_conversations(
        user["id"], project_id=project_id, workspace_id=workspace_id, limit=200
    )
    return jsonify({"status": "ok", "conversations": convs})


@workspace_bp.route("/api/workspace/conversations", methods=["POST"])
@auth.login_required
def api_create_conversation():
    """Create a new (empty) conversation from a JSON body (model/system_prompt/title/project_id/workspace_id/tools)."""
    user = _user()
    data = request.get_json(silent=True) or {}
    conv_id = workspace_db.create_conversation(
        user_id=user["id"],
        model=data.get("model", "gpt-4o"),
        system_prompt=data.get("system_prompt", ""),
        title=data.get("title"),
        project_id=data.get("project_id"),
        workspace_id=data.get("workspace_id") or None,
        tools_enabled=data.get("tools", []),
    )
    workspace_db.log_activity(user["id"], "create_conversation", "conversation", conv_id, {}, _ip())
    return jsonify({"status": "ok", "conversation_id": conv_id})


@workspace_bp.route("/api/workspace/conversations/<int:conv_id>", methods=["GET"])
@auth.login_required
def api_get_conversation(conv_id):
    """Return a conversation with all its messages (Teams-encrypted content decrypted) and citations."""
    user = _user()
    conv = workspace_db.get_conversation(conv_id, user["id"])
    if not conv:
        return jsonify({"status": "error", "message": "Not found"}), 404
    messages  = workspace_db.get_conversation_messages(conv_id)
    citations = {}
    for msg in messages:
        msg["content"] = teams_decrypt(msg["content"] or "")   # no-op if not encrypted
        cits = workspace_db.get_message_citations(msg["id"])
        if cits:
            citations[msg["id"]] = cits
    return jsonify({"status": "ok", "conversation": conv,
                    "messages": messages, "citations": citations})


@workspace_bp.route("/api/workspace/conversations/<int:conv_id>", methods=["PATCH"])
@auth.login_required
def api_update_conversation(conv_id):
    """Update a conversation's title/is_starred/is_archived/system_prompt/model from a JSON body."""
    user = _user()
    data = request.get_json(silent=True) or {}
    allowed = {"title", "is_starred", "is_archived", "system_prompt", "model"}
    update = {k: data[k] for k in allowed if k in data}
    workspace_db.update_conversation(conv_id, update)
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/conversations/<int:conv_id>", methods=["DELETE"])
@auth.login_required
def api_delete_conversation(conv_id):
    """Archive (soft-delete) a conversation."""
    user = _user()
    workspace_db.delete_conversation(conv_id)
    workspace_db.log_activity(user["id"], "delete_conversation", "conversation", conv_id, {}, _ip())
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — WORKSPACES  (team workspace CRUD + member management)
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/users/search", methods=["GET"])
@auth.login_required
def api_search_users():
    """Search users by username/email (``?q=``) for adding to a team workspace; max 8 results."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"status": "ok", "users": []})
    users = workspace_db.search_users(q, limit=8)
    return jsonify({"status": "ok", "users": users})


@workspace_bp.route("/api/workspace/workspaces", methods=["GET"])
@auth.login_required
def api_list_workspaces():
    """List workspaces the current user owns or is a member of."""
    user = _user()
    workspaces = workspace_db.get_workspaces(user["id"])
    return jsonify({"status": "ok", "workspaces": workspaces})


@workspace_bp.route("/api/workspace/workspaces", methods=["POST"])
@auth.login_required
def api_create_workspace():
    """Create a workspace and add the creator as its "owner" member.

    Only admins/devs may set ``is_team=true``; a missing/blank ``name``
    returns 400.
    """
    user = _user()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"status": "error", "message": "name is required"}), 400
    if bool(data.get("is_team", False)) and user["role"] not in ("admin", "dev"):
        return jsonify({"status": "error",
                        "message": "Only administrators can create team workspaces"}), 403
    ws_id = workspace_db.create_workspace(
        user_id=user["id"],
        name=name,
        description=data.get("description", ""),
        color=data.get("color", "#6366f1"),
        icon=data.get("icon", "fa-users"),
        is_team=bool(data.get("is_team", False)),
    )
    # Auto-add creator as owner member
    workspace_db.add_workspace_member(ws_id, user["id"], role="owner")
    workspace_db.log_activity(user["id"], "create_workspace", "workspace", ws_id,
                               {"name": name, "is_team": data.get("is_team")}, _ip())
    return jsonify({"status": "ok", "workspace_id": ws_id})


@workspace_bp.route("/api/workspace/workspaces/<int:ws_id>", methods=["PATCH"])
@auth.login_required
def api_update_workspace(ws_id):
    """Update a workspace's fields; requires the caller to be a member/owner (404 otherwise)."""
    user = _user()
    if not workspace_db.is_workspace_member(ws_id, user["id"]):
        return jsonify({"status": "error", "message": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    workspace_db.update_workspace(ws_id, data)
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/workspaces/<int:ws_id>", methods=["DELETE"])
@auth.login_required
def api_delete_workspace(ws_id):
    """Archive (soft-delete) a workspace; requires the caller to be a member/owner (404 otherwise)."""
    user = _user()
    if not workspace_db.is_workspace_member(ws_id, user["id"]):
        return jsonify({"status": "error", "message": "Not found"}), 404
    workspace_db.delete_workspace(ws_id)
    workspace_db.log_activity(user["id"], "delete_workspace", "workspace", ws_id, {}, _ip())
    return jsonify({"status": "ok"})


# ── Members ────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/workspaces/<int:ws_id>/members", methods=["GET"])
@auth.login_required
def api_list_members(ws_id):
    """List a workspace's members; requires the caller to be a member/owner (404 otherwise)."""
    user = _user()
    if not workspace_db.is_workspace_member(ws_id, user["id"]):
        return jsonify({"status": "error", "message": "Not found"}), 404
    members = workspace_db.get_workspace_members(ws_id)
    return jsonify({"status": "ok", "members": members})


@workspace_bp.route("/api/workspace/workspaces/<int:ws_id>/members", methods=["POST"])
@auth.login_required
def api_add_member(ws_id):
    """Add a member to a workspace, resolved by ``user_id`` or by ``username`` lookup.

    Requires the caller to already be a member/owner (404 otherwise).
    """
    user = _user()
    if not workspace_db.is_workspace_member(ws_id, user["id"]):
        return jsonify({"status": "error", "message": "Not found"}), 404
    data = request.get_json(silent=True) or {}

    # Accept user_id directly or look up by username
    target_user_id = data.get("user_id")
    username = (data.get("username") or "").strip()
    if not target_user_id and username:
        target = auth.get_user_by_username(username)
        if not target:
            return jsonify({"status": "error", "message": f"User '{username}' not found"}), 404
        target_user_id = target["id"]
    if not target_user_id:
        return jsonify({"status": "error", "message": "user_id or username required"}), 400

    role = data.get("role", "member")
    ok = workspace_db.add_workspace_member(ws_id, target_user_id, role)
    if not ok:
        return jsonify({"status": "error", "message": "Could not add member"}), 500
    workspace_db.log_activity(user["id"], "add_member", "workspace", ws_id,
                               {"target_user_id": target_user_id, "role": role}, _ip())
    members = workspace_db.get_workspace_members(ws_id)
    return jsonify({"status": "ok", "members": members})


@workspace_bp.route("/api/workspace/workspaces/<int:ws_id>/members/<int:target_user_id>",
                    methods=["PATCH"])
@auth.login_required
def api_update_member_role(ws_id, target_user_id):
    """Update a workspace member's role; requires the caller to be a member/owner (404 otherwise)."""
    user = _user()
    if not workspace_db.is_workspace_member(ws_id, user["id"]):
        return jsonify({"status": "error", "message": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    role = data.get("role", "member")
    workspace_db.update_workspace_member_role(ws_id, target_user_id, role)
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/workspaces/<int:ws_id>/members/<int:target_user_id>",
                    methods=["DELETE"])
@auth.login_required
def api_remove_member(ws_id, target_user_id):
    user = _user()
    if not workspace_db.is_workspace_member(ws_id, user["id"]):
        return jsonify({"status": "error", "message": "Not found"}), 404
    # Owner cannot remove themselves
    if target_user_id == user["id"]:
        return jsonify({"status": "error", "message": "Cannot remove yourself"}), 400
    workspace_db.remove_workspace_member(ws_id, target_user_id)
    workspace_db.log_activity(user["id"], "remove_member", "workspace", ws_id,
                               {"target_user_id": target_user_id}, _ip())
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — PROJECTS
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/projects", methods=["GET"])
@auth.login_required
def api_list_projects():
    user         = _user()
    is_admin     = user.get("role") in ("admin", "dev")
    workspace_id = request.args.get("workspace_id", type=int)
    projects = workspace_db.get_projects(user["id"], workspace_id=workspace_id, is_admin=is_admin)
    return jsonify({"status": "ok", "projects": projects})


@workspace_bp.route("/api/workspace/projects", methods=["POST"])
@auth.login_required
def api_create_project():
    user = _user()
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"status": "error", "message": "name is required"}), 400
    pid = workspace_db.create_project(
        user_id=user["id"],
        name=data["name"],
        description=data.get("description", ""),
        system_prompt=data.get("system_prompt", ""),
        model=data.get("model", "gpt-4o"),
        temperature=float(data.get("temperature", 0.7)),
        is_shared=bool(data.get("is_shared", False)),
    )
    workspace_db.log_activity(user["id"], "create_project", "project", pid, {}, _ip())
    return jsonify({"status": "ok", "project_id": pid})


@workspace_bp.route("/api/workspace/projects/<int:proj_id>", methods=["PUT"])
@auth.login_required
def api_update_project(proj_id):
    user    = _user()
    project = workspace_db.get_project(proj_id)
    if not project:
        return jsonify({"status": "error", "message": "Project not found"}), 404
    if project["owner_id"] != user["id"] and user.get("role") not in ("admin", "dev"):
        return jsonify({"status": "error", "message": "Only the project owner can edit this project"}), 403
    data = request.get_json(silent=True) or {}
    workspace_db.update_project(proj_id, data)
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/projects/<int:proj_id>", methods=["DELETE"])
@auth.login_required
def api_delete_project(proj_id):
    user    = _user()
    project = workspace_db.get_project(proj_id)
    if not project:
        return jsonify({"status": "error", "message": "Project not found"}), 404
    if project["owner_id"] != user["id"] and user.get("role") not in ("admin", "dev"):
        return jsonify({"status": "error", "message": "Only the project owner can delete this project"}), 403
    workspace_db.delete_project(proj_id)
    workspace_db.log_activity(user["id"], "delete_project", "project", proj_id, {}, _ip())
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — PROJECT TOOL CONFIGURATION  (admin only)
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/registry-tools", methods=["GET"])
@auth.login_required
def api_registry_tools():
    """Return the list of configurable workspace tools (admin/dev only)."""
    user = _user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"status": "error", "message": "Admin only"}), 403
    return jsonify({"status": "ok", "tools": openai_svc.CONFIGURABLE_WS_TOOLS})


@workspace_bp.route("/api/workspace/projects/<int:proj_id>/tools", methods=["GET"])
@auth.login_required
def api_get_project_tools(proj_id):
    """Return the extra tools enabled for a project (admin/dev only)."""
    user    = _user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"status": "error", "message": "Admin only"}), 403
    project = workspace_db.get_project(proj_id)
    if not project:
        return jsonify({"status": "error", "message": "Project not found"}), 404
    tools = json.loads(project.get("tools_enabled") or "[]")
    return jsonify({"status": "ok", "tools_enabled": tools})


@workspace_bp.route("/api/workspace/projects/<int:proj_id>/tools", methods=["PUT"])
@auth.login_required
def api_update_project_tools(proj_id):
    """Update the extra tool list for a project (admin/dev only)."""
    user = _user()
    if user.get("role") not in ("admin", "dev"):
        return jsonify({"status": "error", "message": "Admin only"}), 403
    project = workspace_db.get_project(proj_id)
    if not project:
        return jsonify({"status": "error", "message": "Project not found"}), 404
    data  = request.get_json(silent=True) or {}
    tools = data.get("tools", [])
    workspace_db.update_project_tools(proj_id, tools)
    workspace_db.log_activity(user["id"], "update_project_tools", "project", proj_id,
                               {"tools": tools}, _ip())
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — PROJECT FILES  (RAG per project)
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/projects/<int:proj_id>/files", methods=["GET"])
@auth.login_required
def api_list_project_files(proj_id):
    files = workspace_db.get_project_files(proj_id)
    return jsonify({"status": "ok", "files": files})


@workspace_bp.route("/api/workspace/projects/<int:proj_id>/files", methods=["POST"])
@auth.login_required
def api_upload_project_file(proj_id):
    import tempfile, os as _os, uuid as _uuid
    from werkzeug.utils import secure_filename
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    filename = secure_filename(f.filename or "upload")
    ext = _os.path.splitext(filename)[1].lower()
    try:
        from agents.core.knowledge.document_processor import (
            extract_text, SUPPORTED_EXTENSIONS, _chunk_text,
        )
        from agents.core.knowledge.vector_store import get_vector_store
    except ImportError as e:
        return jsonify({"status": "error", "message": f"RAG unavailable: {e}"}), 500
    if ext not in SUPPORTED_EXTENSIONS:
        return jsonify({"status": "error", "message": f"Unsupported file type: {ext}"}), 400

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name
    try:
        text, meta = extract_text(tmp_path)
        if not text:
            return jsonify({"status": "error", "message": meta.get("error", "Could not extract text")}), 422

        raw_chunks = _chunk_text(text)
        doc_id = f"ws_proj_{proj_id}_{_uuid.uuid4().hex[:8]}_{filename}"
        chunks = [
            {"text": c, "metadata": {
                "filename":    filename,
                "file_type":   ext.lstrip("."),
                "client_id":   "",
                "source_name": f"ws_project_{proj_id}",
            }}
            for c in raw_chunks
        ]

        vs = get_vector_store()
        stored = vs.add_chunks(doc_id, chunks)

        file_size = _os.path.getsize(tmp_path)
        fid = workspace_db.add_project_file(
            project_id=proj_id,
            document_id=doc_id,
            filename=filename,
            file_type=ext.lstrip("."),
            file_size=file_size,
            chunk_count=stored,
        )
        return jsonify({"status": "ok", "file_id": fid, "chunks": stored, "filename": filename})
    except Exception as e:
        logger.exception("project file upload error")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


@workspace_bp.route("/api/workspace/projects/<int:proj_id>/files/<int:file_id>", methods=["DELETE"])
@auth.login_required
def api_delete_project_file(proj_id, file_id):
    try:
        from agents.core.knowledge.vector_store import get_vector_store
        doc_id = workspace_db.delete_project_file(file_id)
        if doc_id:
            get_vector_store().delete_document(doc_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# API — FILE TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/extract-file", methods=["POST"])
@auth.login_required
def api_extract_file():
    import tempfile, os as _os
    from werkzeug.utils import secure_filename
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    filename = secure_filename(f.filename or "upload")
    ext = _os.path.splitext(filename)[1].lower()
    try:
        from agents.core.knowledge.document_processor import extract_text, SUPPORTED_EXTENSIONS
    except ImportError:
        return jsonify({"status": "error", "message": "Document processor unavailable"}), 500
    if ext not in SUPPORTED_EXTENSIONS:
        return jsonify({"status": "error", "message": f"Unsupported file type: {ext}"}), 400
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name
    try:
        text, meta = extract_text(tmp_path)
        if not text:
            return jsonify({"status": "error", "message": meta.get("error", "Could not extract text")}), 422
        return jsonify({"status": "ok", "text": text, "filename": filename})
    except Exception as e:
        logger.exception("extract-file error")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# API — ARTIFACTS
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/artifacts", methods=["GET"])
@auth.login_required
def api_list_artifacts():
    user         = _user()
    atype        = request.args.get("type")
    workspace_id = request.args.get("workspace_id", type=int)
    arts = workspace_db.get_artifacts(
        user["id"], artifact_type=atype, workspace_id=workspace_id, limit=100
    )
    return jsonify({"status": "ok", "artifacts": arts})


@workspace_bp.route("/api/workspace/artifacts/<int:art_id>", methods=["GET"])
@auth.login_required
def api_get_artifact(art_id):
    user = _user()
    art  = workspace_db.get_artifact(art_id, user["id"])
    if not art:
        return jsonify({"status": "error", "message": "Not found"}), 404
    return jsonify({"status": "ok", "artifact": art})


@workspace_bp.route("/api/workspace/artifacts", methods=["POST"])
@auth.login_required
def api_save_artifact():
    user = _user()
    data = request.get_json(silent=True) or {}
    if not data.get("content"):
        return jsonify({"status": "error", "message": "content is required"}), 400
    aid = workspace_db.save_artifact(
        user_id=user["id"],
        artifact_type=data.get("artifact_type", "code"),
        title=data.get("title", "Untitled"),
        content=data["content"],
        language=data.get("language"),
        conversation_id=data.get("conversation_id"),
        message_id=data.get("message_id"),
        tags=data.get("tags", []),
    )
    workspace_db.log_activity(user["id"], "save_artifact", "artifact", aid, {}, _ip())
    return jsonify({"status": "ok", "artifact_id": aid})


@workspace_bp.route("/api/workspace/artifacts/<int:art_id>", methods=["DELETE"])
@auth.login_required
def api_delete_artifact(art_id):
    user = _user()
    workspace_db.delete_artifact(art_id, user["id"])
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/artifacts/<int:art_id>", methods=["PATCH"])
@auth.login_required
def api_update_artifact(art_id):
    user = _user()
    data = request.get_json(silent=True) or {}
    workspace_db.update_artifact(
        art_id, user["id"],
        title=data.get("title"),
        content=data.get("content"),
    )
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/artifacts/<int:art_id>/star", methods=["POST"])
@auth.login_required
def api_star_artifact(art_id):
    user = _user()
    workspace_db.toggle_artifact_star(art_id, user["id"])
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — PROMPT LIBRARY
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/prompts", methods=["GET"])
@auth.login_required
def api_list_prompts():
    user         = _user()
    category     = request.args.get("category")
    workspace_id = request.args.get("workspace_id", type=int)
    prompts = workspace_db.get_prompts(
        user["id"], include_shared=True, category=category, workspace_id=workspace_id
    )
    return jsonify({"status": "ok", "prompts": prompts})


@workspace_bp.route("/api/workspace/prompts", methods=["POST"])
@auth.login_required
def api_create_prompt():
    user = _user()
    data = request.get_json(silent=True) or {}
    if not data.get("title") or not data.get("prompt_text"):
        return jsonify({"status": "error", "message": "title and prompt_text required"}), 400
    pid = workspace_db.create_prompt(
        user_id=user["id"],
        title=data["title"],
        prompt_text=data["prompt_text"],
        category=data.get("category", "general"),
        tags=data.get("tags", []),
        is_shared=bool(data.get("is_shared", False)),
    )
    return jsonify({"status": "ok", "prompt_id": pid})


@workspace_bp.route("/api/workspace/prompts/<int:prompt_id>/use", methods=["POST"])
@auth.login_required
def api_use_prompt(prompt_id):
    workspace_db.increment_prompt_use(prompt_id)
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/prompts/<int:prompt_id>", methods=["DELETE"])
@auth.login_required
def api_delete_prompt(prompt_id):
    user = _user()
    workspace_db.delete_prompt(prompt_id, user["id"])
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — ENTERPRISE MEMORY
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/memory", methods=["GET"])
@auth.login_required
def api_list_memory():
    user       = _user()
    scope      = request.args.get("scope")
    admin_all  = request.args.get("admin_all") == "1" and user.get("role") in ("admin", "dev")
    entries    = workspace_db.get_memory(user["id"], scope=scope, admin_all=admin_all)
    return jsonify({"status": "ok", "memory": entries})


@workspace_bp.route("/api/workspace/memory", methods=["POST"])
@auth.login_required
def api_save_memory():
    user = _user()
    data = request.get_json(silent=True) or {}
    if not data.get("memory_key") or not data.get("memory_value"):
        return jsonify({"status": "error", "message": "memory_key and memory_value required"}), 400

    # Admin can assign memory to a specific user (target_user_id)
    # or to a specific workspace (workspace_id for scope='workspace')
    target_user_id  = data.get("target_user_id")
    workspace_id    = data.get("workspace_id")
    if target_user_id and user.get("role") not in ("admin", "dev"):
        target_user_id = None  # non-admins can only create their own memories

    mid = workspace_db.save_memory_entry(
        user_id=user["id"],
        memory_key=data["memory_key"],
        memory_value=data["memory_value"],
        scope=data.get("scope", "user"),
        importance=int(data.get("importance", 5)),
        workspace_id=workspace_id,
        target_user_id=target_user_id,
    )
    return jsonify({"status": "ok", "memory_id": mid})


@workspace_bp.route("/api/workspace/memory/<int:mem_id>", methods=["DELETE"])
@auth.login_required
def api_delete_memory(mem_id):
    user = _user()
    workspace_db.delete_memory_entry(mem_id, user["id"])
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — MESSAGES (feedback, copy, edit)
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/messages/<int:msg_id>/feedback", methods=["POST"])
@auth.login_required
def api_message_feedback(msg_id):
    user = _user()
    data = request.get_json(silent=True) or {}
    rating = data.get("rating", "")
    if rating not in ("thumbs_up", "thumbs_down"):
        return jsonify({"status": "error", "message": "rating must be thumbs_up or thumbs_down"}), 400
    workspace_db.save_feedback(msg_id, user["id"], rating, data.get("comment", ""))
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/messages/<int:msg_id>/copy", methods=["POST"])
@auth.login_required
def api_message_copy(msg_id):
    user = _user()
    data = request.get_json(silent=True) or {}
    workspace_db.log_copy(msg_id, user["id"], data.get("copy_type", "text"))
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/messages/<int:msg_id>/edit", methods=["PATCH"])
@auth.login_required
def api_message_edit(msg_id):
    user = _user()
    data = request.get_json(silent=True) or {}
    new_content = (data.get("content") or "").strip()
    if not new_content:
        return jsonify({"status": "error", "message": "content required"}), 400
    workspace_db.edit_message(msg_id, user["id"], new_content)
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────────
# API — ANALYTICS
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/analytics/summary", methods=["GET"])
@auth.login_required
def api_analytics_summary():
    user = _user()
    days = int(request.args.get("days", 30))
    summary = workspace_db.get_workspace_analytics(user["id"], days=days)
    return jsonify({"status": "ok", **summary})


# ─────────────────────────────────────────────────────────────
# API — SETTINGS
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/settings", methods=["POST"])
@auth.login_required
def api_save_settings():
    user = _user()
    data = request.get_json(silent=True) or {}
    for key, value in data.items():
        workspace_db.save_user_setting(user["id"], key, str(value))
    return jsonify({"status": "ok"})


@workspace_bp.route("/api/workspace/settings", methods=["GET"])
@auth.login_required
def api_get_settings():
    user     = _user()
    settings = workspace_db.get_user_settings(user["id"])
    return jsonify({"status": "ok", "settings": settings})


# ─────────────────────────────────────────────────────────────
# API — ACTIVITY
# ─────────────────────────────────────────────────────────────

@workspace_bp.route("/api/workspace/activity", methods=["GET"])
@auth.login_required
def api_activity():
    user   = _user()
    limit  = int(request.args.get("limit", 100))
    if user["role"] == "admin":
        activity = workspace_db.get_all_activity(limit=limit)
    else:
        activity = workspace_db.get_activity(user["id"], limit=limit)
    return jsonify({"status": "ok", "activity": activity})


