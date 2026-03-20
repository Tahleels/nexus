"""Document-management / RAG tool implementations: ingestion, plain
knowledge-base search, and connector-scoped search with department/
project visibility filtering.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime

def search_documents(query: str, limit: int = 5, **kwargs) -> dict:
    """Keyword (SQL LIKE) search across the legacy ``hub_knowledge_bases`` table.

    Note: this function is NOT registered in ``TOOL_REGISTRY`` (the
    active, registered document-search tool is :func:`search_knowledge`,
    which runs the hybrid BM25 + vector RAG pipeline against the newer
    document/vector store). This function appears to be dead/legacy code
    kept for backward compatibility or pending removal — left as-is per
    instructions, not modified.

    Args:
        query (str): Substring to search for (matched via SQL ``LIKE
            '%query%'`` against content, name, and description columns —
            not true semantic search despite the name).
        limit (int): Maximum number of rows to return. Defaults to 5.
        **kwargs: Recognized keys: ``_db_query`` (callable) — override for
            the DB query function, defaults to :func:`_hub_db_query`;
            ``document_ids`` (str | list) — restrict results to these
            document IDs.

    Returns:
        dict: On success, ``{"success": True, "query": str, "count": int,
        "results": list[dict]}`` where each result has ``id``, ``title``,
        ``type``, ``excerpt`` (truncated to 400 chars), and a constant
        ``score`` of 1.0 (no real ranking is computed). On failure,
        ``{"error": str, "query": str, "results": []}``.
    """
    db_query_fn = kwargs.get('_db_query') or _hub_db_query

    # Optional: filter to configured document IDs
    doc_ids_raw = kwargs.get('document_ids', [])
    if isinstance(doc_ids_raw, str):
        doc_ids = [d.strip() for d in doc_ids_raw.split(',') if d.strip()]
    elif isinstance(doc_ids_raw, list):
        doc_ids = [str(d) for d in doc_ids_raw if d]
    else:
        doc_ids = []

    try:
        if doc_ids:
            placeholders = ','.join(['?' for _ in doc_ids])
            sql = f"""
                SELECT TOP (?) id, name, description, file_type,
                       LEFT(content, 800) AS excerpt
                FROM   hub_knowledge_bases
                WHERE  id IN ({placeholders})
                  AND  (content LIKE ? OR name LIKE ? OR description LIKE ?)
                ORDER BY created_at DESC
            """
            params = (int(limit),) + tuple(doc_ids) + (
                f'%{query}%', f'%{query}%', f'%{query}%')
        else:
            sql = """
                SELECT TOP (?) id, name, description, file_type,
                       LEFT(content, 800) AS excerpt
                FROM   hub_knowledge_bases
                WHERE  content     LIKE ?
                    OR name        LIKE ?
                    OR description LIKE ?
                ORDER BY created_at DESC
            """
            params = (int(limit), f'%{query}%', f'%{query}%', f'%{query}%')

        rows = db_query_fn(sql, params, fetchall=True)

        if rows is None:
            return {"error": "Knowledge base not available.", "query": query, "results": []}

        results = []
        for r in (rows or []):
            excerpt = (r.get('excerpt') or '')
            results.append({
                "id":      r['id'],
                "title":   r['name'],
                "type":    r.get('file_type', 'unknown'),
                "excerpt": excerpt[:400] + ('…' if len(excerpt) > 400 else ''),
                "score":   1.0,
            })

        return {"success": True, "query": query, "count": len(results),
                "results": results}

    except Exception as e:
        return {"error": str(e), "query": query, "results": []}


def process_document(file_path: str, source_name: str = "",
                     client_id: str = "", **kwargs) -> dict:
    """Ingest a document file into the vector knowledge base (RAG).

    Thin wrapper around
    ``agents.core.knowledge.document_processor.process_document``, which
    does the actual chunking, embedding, and storage in ChromaDB + SQL
    Server. Supported formats: PDF, Word, Excel, CSV, PPT, HTML, email
    (.eml/.msg), images, TXT, JSON. The returned ``document_id`` can later
    be passed to :func:`search_knowledge` via its ``document_ids`` filter.

    Args:
        file_path (str): Server-side path to the file to ingest. Typically
            supplied automatically by the chat UI when a user uploads a
            file with "Add to Knowledge Base" mode.
        source_name (str, optional): Display name for the document. Used
            for citation/UI purposes.
        client_id (str, optional): Client/tenant identifier to associate
            with the ingested document, if applicable.
        **kwargs: Orchestrator-injected context (unused directly by this
            tool; forwarded only via the explicit named parameters).

    Returns:
        dict: The dict returned by the underlying document processor on
        success (includes a document_id, among other fields — see
        ``agents.core.knowledge.document_processor.process_document`` for
        the exact shape). On failure, ``{"success": False, "error": str,
        "file_path": str}``.
    """
    try:
        from agents.core.knowledge.document_processor import (
            process_document as _proc_doc,
        )
        return _proc_doc(
            file_path=file_path,
            client_id=client_id,
            source_name=source_name,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc), "file_path": file_path}


def search_knowledge(query: str, n_results: int = 5,
                     client_id: str = "", **kwargs) -> dict:
    """Run hybrid RAG search across ingested documents and the knowledge store.

    Combines BM25 keyword retrieval with vector (semantic) retrieval,
    optional cross-encoder reranking, source confidence scoring, and
    citation generation (via ``agents.core.knowledge.rag_pipeline``).
    Visibility is scoped per-user: non-admin/non-dev users only see
    documents visible to them (``document_processor.get_visible_doc_ids``),
    optionally intersected with an explicit ``document_ids`` allowlist
    configured on the agent. Also appends matching persistent
    knowledge-store entries (key/value pairs, score >= 0.35) and any prior
    session context for follow-up awareness.

    Usage: call this tool BEFORE answering any question that may be
    covered by uploaded documents. Use the returned ``context`` field as
    reference material and cite the source name as ``[Source N]`` when
    quoting. If confidence is too low, the context block instructs the
    LLM to say "Insufficient Evidence" rather than hallucinate.

    Args:
        query (str): Natural-language question or keywords to search for.
        n_results (int): Maximum number of document results to return.
            Defaults to 5.
        client_id (str, optional): Present for signature compatibility;
            not currently used to scope the search (visibility scoping is
            done via user_id/role instead).
        **kwargs: Tool-config / orchestrator-injected context. Recognized
            keys: ``_hub_ctx`` (dict) — hub session context, used to
            resolve ``user.id``/``user.role`` and ``session_id`` for
            visibility scoping and follow-up context; ``document_ids``
            (str | list) — restrict/pin search to these document IDs
            (intersected with what the user can see, unless the user is
            admin/dev).

    Returns:
        dict: On success, ``{"success": True, "query": str, "context":
        str, "citations": list, "confidence": int (0-100), "fallback":
        bool, "found": bool, "documents": list, "knowledge": list,
        "doc_count": int, "from_cache": bool, "latency_ms": int}``. On
        failure, ``{"success": False, "error": str, "query": str,
        "documents": [], "knowledge": [], "context": "", "fallback":
        False, "citations": []}``.
    """
    import sys as _sys
    try:
        from agents.core.knowledge.rag_pipeline import get_pipeline
        from agents.core.knowledge.document_processor import get_visible_doc_ids
        from agents.core.knowledge.vector_store import get_vector_store

        # ── Resolve user context from hub session ────────────────────────────
        hub_ctx  = kwargs.get("_hub_ctx") or {}
        hub_user = hub_ctx.get("user") or {}
        user_id  = hub_user.get("id") or 0
        user_role = (hub_user.get("role") or "user").lower()
        session_id = hub_ctx.get("session_id") or ""

        # ── Resolve allowed doc_ids filter (explicit tool config or user scope) ─
        raw_ids = kwargs.get("document_ids", [])
        if isinstance(raw_ids, str):
            explicit_ids = {d.strip() for d in raw_ids.split(",") if d.strip()}
        elif isinstance(raw_ids, list):
            explicit_ids = {str(d) for d in raw_ids if d}
        else:
            explicit_ids = set()

        if explicit_ids:
            if user_id and user_role not in ("admin", "dev"):
                # Intersect agent-pinned doc list with what this user can actually see
                visible = get_visible_doc_ids(user_id, user_role)
                doc_ids_set = explicit_ids & set(visible) if visible is not None else explicit_ids
            else:
                doc_ids_set = explicit_ids
        elif user_id and user_role not in ("admin", "dev"):
            visible = get_visible_doc_ids(user_id, user_role)
            doc_ids_set = set(visible) if visible is not None else None
        else:
            doc_ids_set = None  # admin/dev see all

        # ── Run hybrid RAG pipeline ───────────────────────────────────────────
        pipeline = get_pipeline()
        rag_out  = pipeline.run(
            query=query,
            doc_ids=doc_ids_set,
            pinned_doc_ids=explicit_ids if explicit_ids else None,
            n_results=n_results,
            user_id=user_id,
            session_id=session_id,
        )

        context    = rag_out.get("context", "")
        citations  = rag_out.get("citations", [])
        confidence = rag_out.get("confidence", 0)
        fallback   = rag_out.get("fallback", False)
        results    = rag_out.get("results", [])

        # ── Append knowledge-store entries (key/value, not doc chunks) ────────
        try:
            vs   = get_vector_store()
            know = vs.search_knowledge(query, n_results=3)
            know = [k for k in know if k.get("score", 0) >= 0.35]
            if know and not fallback:
                kparts = [
                    f"[Knowledge: {k['key']}]\n{k['value']}"
                    for k in know if k.get("key") and k.get("value")
                ]
                if kparts:
                    context += "\n\n=== KNOWLEDGE BASE ENTRIES ===\n\n" + "\n\n---\n\n".join(kparts)
        except Exception:
            know = []
        else:
            pass

        # ── Prior session context (follow-up awareness) ───────────────────────
        prior = rag_out.get("session_context", "")
        if prior:
            context = prior + "\n\n" + context

        return {
            "success":    True,
            "query":      query,
            "context":    context,
            "citations":  citations,
            "confidence": round(confidence * 100),
            "fallback":   fallback,
            "found":      not fallback and len(results) > 0,
            "documents":  results,
            "knowledge":  know if not fallback else [],
            "doc_count":  len(results),
            "from_cache": rag_out.get("from_cache", False),
            "latency_ms": rag_out.get("latency_ms", 0),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "query": query,
                "documents": [], "knowledge": [], "context": "",
                "fallback": False, "citations": []}


def _apply_department_scope_filter(doc_ids: set, user_id: int, agent_id,
                                    active_scope_type=None, active_scope_id=None) -> set:
    """Narrow a connector's raw document set down to what this user is
    actually allowed to see through this agent, in this sidebar context.

    - ``scope='global'`` documents always pass.
    - ``scope='user'`` documents pass only for the uploading user.
    - Documents directly granted to this user via ``app_document_assignments``
      (an admin sharing a specific document with someone outside its normal
      department/project) always pass, regardless of scope — that grant is
      an explicit override and shouldn't be undone by department mismatches.
    - ``scope='department'``/``'project'`` documents (not directly assigned)
      pass only if the department/project is one the AGENT is tagged with
      (an agent with no department/project tags is treated as unrestricted
      on that axis), one the USER belongs to, and — when the caller opened
      the agent under a specific department/project context (the sidebar
      selector) — matches that active context too. All three checks must
      agree; any one of them excluding a document is enough to hide it.
    """
    if not doc_ids:
        return doc_ids

    from database.app_db import get_app_db
    with get_app_db() as conn:
        cur = conn.cursor()
        ph = ",".join("?" for _ in doc_ids)
        cur.execute(
            f"SELECT id, scope, scope_id FROM app_documents WHERE id IN ({ph})",
            *doc_ids)
        doc_scopes = {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}

        cur.execute("SELECT dept_id FROM user_departments WHERE user_id = ?", user_id)
        user_dept_ids = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT project_id FROM user_projects WHERE user_id = ?", user_id)
        user_proj_ids = {r[0] for r in cur.fetchall()}

        cur.execute(
            f"SELECT doc_id FROM app_document_assignments "
            f"WHERE user_id = ? AND doc_id IN ({ph})",
            user_id, *doc_ids)
        assigned_ids = {str(r[0]) for r in cur.fetchall()}

    agent_dept_ids, agent_proj_ids = set(), set()
    if agent_id:
        _org = sys.modules.get("org_db")
        if _org:
            info = _org.get_resource_orgs_batch("hub_agent", [agent_id]).get(str(agent_id), {})
            agent_dept_ids = set(info.get("dept_ids") or [])
            agent_proj_ids = set(info.get("project_ids") or [])

    keep = set(assigned_ids)
    for doc_id in doc_ids:
        if doc_id in assigned_ids:
            continue
        scope, scope_id = doc_scopes.get(doc_id, (None, None))
        if not scope or scope == "global":
            keep.add(doc_id)
        elif scope == "user":
            if scope_id == user_id:
                keep.add(doc_id)
        elif scope == "department":
            if (not agent_dept_ids or scope_id in agent_dept_ids) \
                    and scope_id in user_dept_ids \
                    and (active_scope_type != "department" or active_scope_id in (None, scope_id)):
                keep.add(doc_id)
        elif scope == "project":
            if (not agent_proj_ids or scope_id in agent_proj_ids) \
                    and scope_id in user_proj_ids \
                    and (active_scope_type != "project" or active_scope_id in (None, scope_id)):
                keep.add(doc_id)
        else:
            keep.add(doc_id)
    return keep


def search_connector_knowledge(query: str, n_results: int = 5,
                               connector_keys=None,
                               filter_doc_ids=None,
                               filter_document_name: str = None, **kwargs) -> dict:
    """Run RAG search scoped to documents ingested from specific connectors.

    Resolves the document IDs belonging to the given connector(s) by
    looking up ``app_documents`` rows whose ``source_watch_id`` /
    ``source_watch_type`` match the parsed ``connector_keys``, narrows that
    set through :func:`_apply_department_scope_filter` (department/project/
    user scope vs. the agent's own tags, the querying user's own org
    membership, and the active sidebar department/project context — see
    that function's docstring), then runs the same hybrid RAG pipeline as
    :func:`search_knowledge` restricted to what remains. This filtering
    applies to every user including admin/dev — there is no bypass. When no
    connector keys are configured at all, falls back to the whole knowledge
    base (via ``document_processor.get_visible_doc_ids``, forced through its
    non-admin branch) narrowed by the same
    :func:`_apply_department_scope_filter` pass — so admin/dev get no
    special-cased bypass here either.

    Args:
        query (str): Question or keywords to search for.
        n_results (int): Maximum number of results to return. Defaults
            to 5.
        connector_keys (list[str], optional): List of connector keys in
            the form ``"filesystem:<id>"`` / ``"sharepoint:<id>"``. If not
            passed directly, falls back to the ``connector_keys`` entry
            of the ``_tool_config`` kwarg (pre-filled by agent config).
        **kwargs: Tool-config / orchestrator-injected context. Recognized
            keys: ``_tool_config`` (dict) — may contain ``connector_keys``;
            ``_hub_ctx`` (dict) — hub session context for user id/role and
            session_id.

    Returns:
        dict: On success, ``{"success": True, "context": str, "citations":
        list, "connector_keys": list[str], "result_count": int}``. If the
        resolved connector(s) have no ingested documents yet, returns
        ``{"success": True, "context": str (explanatory message),
        "citations": [], "connector_keys": list[str]}`` without calling
        the RAG pipeline. On failure, ``{"success": False, "error": str}``
        (also logged via the "app" logger).
    """
    try:
        from agents.core.knowledge.rag_pipeline import get_pipeline
        from agents.core.knowledge.document_processor import get_visible_doc_ids

        hub_ctx   = kwargs.get("_hub_ctx") or {}
        hub_user  = hub_ctx.get("user") or {}
        user_id   = hub_user.get("id") or 0
        session_id = hub_ctx.get("session_id") or ""
        agent_id   = hub_ctx.get("agent_id")
        active_scope_type = hub_ctx.get("scope_type")
        active_scope_id   = hub_ctx.get("scope_id")

        # Resolve connector_keys from arg or tool config
        raw_keys = connector_keys or kwargs.get("_tool_config", {}).get("connector_keys", [])
        if isinstance(raw_keys, str):
            raw_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        keys = [k for k in (raw_keys or []) if k and ":" in k]

        doc_ids_set = None

        if keys:
            collected = set()
            from database.app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                for key in keys:
                    watch_type, watch_id_str = key.split(":", 1)
                    try:
                        watch_id = int(watch_id_str)
                        cur.execute("""
                            SELECT id FROM app_documents
                            WHERE source_watch_id = ? AND source_watch_type = ?
                        """, watch_id, watch_type)
                        collected.update(str(r[0]) for r in cur.fetchall())
                    except Exception as exc:
                        import logging as _l
                        _l.getLogger("app").warning(
                            "search_connector_knowledge: filter error for %s: %s", key, exc)
            doc_ids_set = _apply_department_scope_filter(
                collected, user_id, agent_id, active_scope_type, active_scope_id)
        elif user_id:
            # No connectors attached — fall back to the whole knowledge base,
            # but apply the exact same scoping as the connector-attached path:
            # global docs always visible, scope='user' docs owner-only,
            # department/project docs need the agent's own tags + the user's
            # own membership + the active sidebar context all to agree. Force
            # get_visible_doc_ids through its non-admin branch (pass "user"
            # regardless of the caller's real role) so admin/dev get no
            # special-cased bypass here either.
            visible = get_visible_doc_ids(user_id, "user")
            doc_ids_set = _apply_department_scope_filter(
                set(visible or []), user_id, agent_id, active_scope_type, active_scope_id)

        # Resolve filter_document_name → doc_id by searching document_profiles by name/summary
        name_filter = filter_document_name or kwargs.get("filter_document_name")
        if name_filter and name_filter.strip():
            import json as _json
            name_lower = name_filter.strip().lower()
            candidate_keys = list(doc_ids_set) if doc_ids_set else None
            with get_app_db() as _conn:
                _cur = _conn.cursor()
                if candidate_keys:
                    ph = ",".join("?" for _ in candidate_keys)
                    _cur.execute(
                        f"SELECT doc_id, key_metrics FROM document_profiles WHERE doc_id IN ({ph})",
                        *candidate_keys)
                else:
                    _cur.execute("SELECT doc_id, key_metrics FROM document_profiles")
                matched_ids = set()
                for _doc_id, _km_raw in _cur.fetchall():
                    _km_str = (_km_raw or "").lower()
                    try:
                        _km_obj = _json.loads(_km_raw or "{}")
                        _km_str = " ".join(str(v) for v in _km_obj.values() if v).lower()
                    except Exception:
                        pass
                    if name_lower in _km_str:
                        matched_ids.add(_doc_id)
            if matched_ids:
                doc_ids_set = (doc_ids_set & matched_ids) if doc_ids_set else matched_ids

        # Narrow to caller-specified doc_ids (Stage 2 after list_connector_documents)
        raw_filter = filter_doc_ids or kwargs.get("filter_doc_ids", [])
        is_scoped = False
        if raw_filter:
            if isinstance(raw_filter, str):
                raw_filter = [d.strip() for d in raw_filter.split(",") if d.strip()]
            filter_set = {str(d) for d in raw_filter if d}
            if filter_set:
                doc_ids_set = (doc_ids_set & filter_set) if doc_ids_set else filter_set
                is_scoped = True

        # Treat a name-filtered search as scoped too
        if name_filter and name_filter.strip():
            is_scoped = True

        if doc_ids_set is not None and len(doc_ids_set) == 0:
            return {
                "success":        True,
                "context":        "No documents have been ingested from the selected connector(s) yet.",
                "citations":      [],
                "connector_keys": keys,
            }

        pipeline = get_pipeline()
        rag_out  = pipeline.run(
            query=query,
            doc_ids=doc_ids_set,
            n_results=n_results,
            user_id=user_id,
            session_id=session_id,
            scoped=is_scoped,
        )

        context   = rag_out.get("context", "")
        citations = rag_out.get("citations", [])
        return {
            "success":        True,
            "context":        context,
            "citations":      citations,
            "connector_keys": keys,
            "result_count":   len(citations),
        }
    except Exception as exc:
        import logging as _l
        _l.getLogger("app").exception("search_connector_knowledge error")
        return {"success": False, "error": str(exc)}


def _rank_profiles_by_query(rows: list, query: str, n_results: int) -> list:
    """Embed `query` and rank `rows` (each having a summary_embedding JSON
    string) by cosine similarity, returning the top n_results. Falls back to
    returning all rows unranked if embedding fails for any reason."""
    try:
        from agents.core.knowledge.vector_store import _embed_one, _cosine_top_k
        q_emb = _embed_one(query)
        ranked = _cosine_top_k(q_emb, rows, "summary_embedding", n_results)
        return ranked
    except Exception:
        return rows[:n_results]


def list_connector_documents(connector_keys=None, query: str = "",
                             n_results: int = 10, **kwargs) -> dict:
    """List documents ingested from one or more connectors with their summaries.

    Use this as Stage 1 before searching: retrieve a compact summary for every
    document in the connector(s), identify which ones are relevant to the
    user's query, then call search_connector_knowledge (with filter_doc_ids)
    or search_knowledge (with document_ids) for deep content on only those
    files.

    Args:
        connector_keys (list[str] | str, optional): Connector key(s), e.g.
            ["sharepoint:5"] or "sharepoint:5". Pre-filled by agent config
            (same config field as search_connector_knowledge) if not passed
            directly.
        query (str, optional): If provided, ranks documents by embedding
            similarity to this text (e.g. the JD or a key-skills summary)
            and returns only the top n_results — every document is still
            compared (no chunking, one embedding per document), just not
            all summaries are sent back. Omit to return every document
            unranked (e.g. for an audit/full listing).
        n_results (int): Max documents to return when query is given.
            Defaults to 20. Ignored when query is omitted.

    Returns:
        dict: {"success": True, "connector_keys": list[str], "total": int,
               "documents": [{"doc_id", "source_file", "summary",
               "key_metrics", "document_type", "processed_at",
               "connector_key"}, ...]}
    """
    import json as _json
    try:
        from database.app_db import get_app_db

        raw_keys = connector_keys or kwargs.get("_tool_config", {}).get("connector_keys", [])
        if isinstance(raw_keys, str):
            keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        elif isinstance(raw_keys, list):
            keys = [str(k) for k in raw_keys if k]
        else:
            keys = []

        if not keys:
            return {"success": False, "error": "No connector_keys provided or configured."}

        placeholders = ",".join(["?" for _ in keys])
        select_cols = ("doc_id, source_file, summary, key_metrics, document_type, "
                       "processed_at, connector_key" + (", summary_embedding" if query else ""))
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT {select_cols}
                FROM document_profiles
                WHERE connector_key IN ({placeholders})
                ORDER BY processed_at DESC
            """, *keys)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Same department/user/project scope filter as search_connector_knowledge —
        # this tool exposes document names/summaries, which is its own leak surface
        # even without returning chunk content.
        hub_ctx   = kwargs.get("_hub_ctx") or {}
        hub_user  = hub_ctx.get("user") or {}
        _uid      = hub_user.get("id") or 0
        _visible  = _apply_department_scope_filter(
            {r["doc_id"] for r in rows}, _uid, hub_ctx.get("agent_id"),
            hub_ctx.get("scope_type"), hub_ctx.get("scope_id"))
        rows = [r for r in rows if r["doc_id"] in _visible]

        total_found = len(rows)

        if query and query.strip():
            rows = _rank_profiles_by_query(rows, query.strip(), n_results)
            for row in rows:
                row.pop("summary_embedding", None)
                row.pop("_score", None)

        for row in rows:
            if row.get("key_metrics"):
                try:
                    row["key_metrics"] = _json.loads(row["key_metrics"])
                except Exception:
                    pass
            if row.get("processed_at"):
                row["processed_at"] = str(row["processed_at"])

        return {
            "success":        True,
            "connector_keys": keys,
            "total":          len(rows),
            "total_in_connector": total_found,
            "doc_ids":        [r["doc_id"] for r in rows],
            "documents":      rows,
        }
    except Exception as exc:
        import logging as _l
        _l.getLogger("app").exception("list_connector_documents error")
        return {"success": False, "error": str(exc)}


def list_knowledge_documents(document_ids=None, query: str = "",
                             n_results: int = 20, **kwargs) -> dict:
    """List directly-attached knowledge documents with their summaries.

    Use this as Stage 1 before searching: retrieve a compact summary for every
    document connected to this agent, identify which ones are relevant, then
    call search_knowledge with those specific document_ids for deep content.

    Args:
        document_ids (list[str] | str, optional): Doc IDs to list. Falls back
            to the agent's configured document_ids from _tool_config.
        query (str, optional): If provided, ranks documents by embedding
            similarity to this text and returns only the top n_results —
            every document is still compared, just not all summaries sent
            back. Omit to return every document unranked.
        n_results (int): Max documents to return when query is given.
            Defaults to 20. Ignored when query is omitted.

    Returns:
        dict: {"success": True, "total": int,
               "documents": [{"doc_id", "source_file", "summary",
               "key_metrics", "document_type", "processed_at"}, ...]}
    """
    import json as _json
    try:
        from database.app_db import get_app_db

        raw_ids = document_ids or kwargs.get("_tool_config", {}).get("document_ids", [])
        if isinstance(raw_ids, str):
            ids = [d.strip() for d in raw_ids.split(",") if d.strip()]
        elif isinstance(raw_ids, list):
            ids = [str(d) for d in raw_ids if d]
        else:
            ids = []

        if not ids:
            return {"success": False, "error": "No document_ids provided or configured."}

        placeholders = ",".join(["?" for _ in ids])
        select_cols = ("doc_id, source_file, summary, key_metrics, document_type, "
                       "processed_at" + (", summary_embedding" if query else ""))
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT {select_cols}
                FROM document_profiles
                WHERE doc_id IN ({placeholders})
                ORDER BY processed_at DESC
            """, *ids)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        total_found = len(rows)

        if query and query.strip():
            rows = _rank_profiles_by_query(rows, query.strip(), n_results)
            for row in rows:
                row.pop("summary_embedding", None)
                row.pop("_score", None)

        for row in rows:
            if row.get("key_metrics"):
                try:
                    row["key_metrics"] = _json.loads(row["key_metrics"])
                except Exception:
                    pass
            if row.get("processed_at"):
                row["processed_at"] = str(row["processed_at"])

        return {
            "success":            True,
            "total":              len(rows),
            "total_in_selection": total_found,
            "doc_ids":            [r["doc_id"] for r in rows],
            "documents":          rows,
        }
    except Exception as exc:
        import logging as _l
        _l.getLogger("app").exception("list_knowledge_documents error")
        return {"success": False, "error": str(exc)}
