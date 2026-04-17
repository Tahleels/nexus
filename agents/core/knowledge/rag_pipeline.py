"""
rag_pipeline.py — Enterprise-grade RAG pipeline.

Pipeline steps
--------------
1.  Dedup / Normalize     — skip duplicate queries via cache; normalise whitespace
2.  Hybrid Retrieval      — BM25 keyword + vector (cosine) combined via RRF
3.  ANN + Reranking       — Cross-Encoder reranker (optional, soft-fail if not installed)
4.  Source Confidence     — freshness × trust × retrieval-consistency score per chunk
5.  Constrained Context   — build a context block with source citations
6.  Hallucination Check   — if max confidence < threshold → "Insufficient Evidence"
7.  Session Memory        — per-session last-N results for follow-up context
8.  Cache write-through   — store results keyed by (query_hash, user_id)
9.  Retrieval Trace       — async write to app_retrieval_traces for observability
10. Structured Response   — returns {context, citations, confidence, fallback, results}

Typical usage (inside an agent tool or blueprint route):
    from agents.core.knowledge.rag_pipeline import RAGPipeline
    pipe = RAGPipeline()
    out  = pipe.run(query, doc_ids=visible_ids, user_id=uid, session_id=sid)
    if out["fallback"]:
        # Tell the LLM: insufficient evidence
        ...
    else:
        # Inject out["context"] into the system prompt and include out["citations"]
        ...
"""

from __future__ import annotations

import hashlib
import json
from logging_config import get_logger
import math
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

logger = get_logger(__name__)

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Defaults ──────────────────────────────────────────────────────────────────
_CONFIDENCE_THRESHOLD = float(os.environ.get("RAG_CONFIDENCE_THRESHOLD", "0.20"))
_CACHE_TTL_SECONDS    = int(os.environ.get("RAG_CACHE_TTL", "300"))      # 5 min
_HYBRID_ALPHA         = float(os.environ.get("RAG_HYBRID_ALPHA", "0.6")) # semantic weight
_MAX_CONTEXT_CHARS    = int(os.environ.get("RAG_MAX_CONTEXT_CHARS", "12000"))
_SESSION_MEMORY_LEN   = 3                                                  # last-N turns
_RERANK_TOP_K         = 20   # candidates to rerank before taking top-N (unscoped search only)
# When doc_ids explicitly scopes the search (e.g. Stage 2 deep search after a
# Stage 1 shortlist), completeness matters more than speed — this is the
# safety ceiling for that case, not the everyday default.
_SCOPED_MAX_CONTEXT_CHARS = int(os.environ.get("RAG_SCOPED_MAX_CONTEXT_CHARS", "200000"))
# Per-document excerpt count for scoped (doc_ids) searches — every document
# in scope gets its own best-matching chunk(s), not its full text and not a
# single global top-N that could exclude weaker-but-still-relevant documents.
_SCOPED_CHUNKS_PER_DOC = int(os.environ.get("RAG_SCOPED_CHUNKS_PER_DOC", "2"))


def _total_chunk_count(doc_ids) -> int:
    """Sum chunk_count for a set of document IDs (0 if empty or on error)."""
    if not doc_ids:
        return 0
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            ph = ",".join("?" for _ in doc_ids)
            cur.execute(
                f"SELECT COALESCE(SUM(chunk_count), 0) FROM app_documents WHERE id IN ({ph})",
                *doc_ids)
            return cur.fetchone()[0] or 0
    except Exception:
        return 0

# File-type trust scores (0-1): higher = more trusted source
_FILE_TRUST = {
    "pdf": 0.90, "docx": 0.90, "doc": 0.85,
    "xlsx": 0.80, "xls": 0.80, "xlsm": 0.80, "csv": 0.70,
    "pptx": 0.75, "ppt": 0.70,
    "txt": 0.65, "md": 0.65,
    "html": 0.55, "htm": 0.55,
    "json": 0.70,
    "eml": 0.60, "msg": 0.60,
    "png": 0.40, "jpg": 0.40, "jpeg": 0.40,
}

# ── BM25 helper ───────────────────────────────────────────────────────────────

def _bm25_scores(query: str, corpus: List[str]) -> List[float]:
    """
    Compute BM25 scores for each document in corpus against query.
    Falls back to simple TF scoring if rank_bm25 is not installed.
    """
    if not corpus:
        return []
    tokens = query.lower().split()
    try:
        from rank_bm25 import BM25Okapi
        tokenized = [doc.lower().split() for doc in corpus]
        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(tokens).tolist()
    except ImportError:
        # Fallback: simple TF hit-count normalised to [0,1]
        scores = []
        for doc in corpus:
            doc_lower = doc.lower()
            hits = sum(doc_lower.count(t) for t in tokens)
            scores.append(float(hits))
        mx = max(scores) if scores else 1.0
        scores = [s / mx if mx else 0.0 for s in scores]
    return scores


# ── Cross-encoder reranker ─────────────────────────────────────────────────────

_reranker = None
_reranker_tried = False


def _get_reranker():
    """
    Lazily load and cache the cross-encoder reranker model for this process.

    Only attempts the import/load once per process (`_reranker_tried` guard);
    if `sentence_transformers` is missing or loading fails, logs once and
    returns None on every subsequent call instead of retrying.

    Returns:
        The loaded CrossEncoder instance, or None if unavailable.
    """
    global _reranker, _reranker_tried
    if _reranker_tried:
        return _reranker
    _reranker_tried = True
    try:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        logger.info("RAGPipeline: cross-encoder reranker loaded")
    except Exception as exc:
        logger.info("RAGPipeline: reranker unavailable (%s) — skipping rerank step", exc)
    return _reranker


def _rerank(query: str, candidates: List[Dict]) -> List[Dict]:
    """
    Rerank candidates with cross-encoder. Returns same list with updated scores.
    Silently skips if model unavailable.
    """
    reranker = _get_reranker()
    if not reranker or not candidates:
        return candidates
    try:
        pairs  = [(query, c["text"]) for c in candidates]
        scores = reranker.predict(pairs).tolist()
        for c, s in zip(candidates, scores):
            c["rerank_score"] = round(float(s), 4)
        candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
    except Exception as exc:
        logger.debug("RAGPipeline._rerank failed: %s", exc)
    return candidates


def _per_document_top_k(candidates: List[Dict], k: int) -> List[Dict]:
    """
    Group ranked candidates by document_id and keep each document's top k
    (by rerank/hybrid score), instead of a single global top-N.

    This is what makes a doc_ids-scoped Stage 2 search actual RAG rather than
    a raw dump: every scoped document is still represented (nothing excluded
    wholesale), but only its best-matching excerpt(s) are sent on — not its
    full text, and not crowded out by a more verbose or more similar-sounding
    document elsewhere in the shortlist.
    """
    from collections import defaultdict
    by_doc: Dict[str, List[Dict]] = defaultdict(list)
    for c in candidates:
        by_doc[c.get("document_id")].append(c)

    selected = []
    for doc_chunks in by_doc.values():
        selected.extend(doc_chunks[:k])   # candidates already sorted by score going in

    selected.sort(key=lambda x: x.get("rerank_score", x.get("score", 0)), reverse=True)
    return [_sanitize_chunk(c) for c in selected]


# ── Confidence scoring ────────────────────────────────────────────────────────

def _freshness_score(created_at) -> float:
    """Exponential decay: full score for docs < 30 days, ~0.5 at 180 days."""
    try:
        if isinstance(created_at, str):
            from datetime import datetime
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00").replace(" ", "T"))
        age_days = (datetime.utcnow() - created_at.replace(tzinfo=None)).days
        return math.exp(-age_days / 180.0)
    except Exception:
        return 0.5


def _consistency_score(top_chunks: List[Dict]) -> float:
    """
    Retrieval consistency: how many distinct documents agree on an answer.
    Returns [0, 1] — scales linearly from 1 source (0.33) to 3+ sources (1.0).
    A single source gets 0.33 rather than 0 so it still has a fighting chance.
    """
    distinct = len({c.get("document_id") for c in top_chunks if c.get("document_id")})
    return round(min(1.0, distinct / 3.0), 4)


def _source_confidence(chunk: Dict, retrieval_score: float,
                       consistency: float = 1.0) -> float:
    """
    Composite confidence = retrieval × trust × freshness × consistency_weight.
    All components are in [0, 1].
    consistency_weight blends consistency signal: 70% base + 30% from consistency.
    """
    file_type   = (chunk.get("file_type") or chunk.get("metadata", {}).get("file_type") or "").lower()
    trust       = _FILE_TRUST.get(file_type, 0.60)
    freshness   = _freshness_score(chunk.get("created_at")) if chunk.get("created_at") else 0.7
    cons_weight = 0.7 + 0.3 * consistency
    return round(retrieval_score * trust * freshness * cons_weight, 4)


# ── Citation builder ──────────────────────────────────────────────────────────

def _sanitize_chunk(chunk: Dict) -> Dict:
    """Convert any non-JSON-serializable values (datetime, etc.) to strings."""
    from datetime import datetime as _dt
    out = {}
    for k, v in chunk.items():
        if isinstance(v, _dt):
            out[k] = v.isoformat()
        elif hasattr(v, 'isoformat'):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _build_citation(chunk: Dict, index: int) -> str:
    """
    Format a single citation string for a result chunk.

    Args:
        chunk: Result chunk dict (filename/source_name, metadata.chunk_index,
            created_at).
        index: 1-based citation number matching the [Source N] markers used
            in the context block.

    Returns:
        A string like "[1] report.pdf | chunk 3 | 2026-01-15" (page/date
        segments omitted when not available).
    """
    fname   = chunk.get("filename") or chunk.get("source_name") or "Unknown"
    page    = chunk.get("metadata", {}).get("chunk_index")
    created = str(chunk.get("created_at", ""))[:10] if chunk.get("created_at") else ""
    parts   = [f"[{index}] {fname}"]
    if page is not None:
        parts.append(f"chunk {page}")
    if created:
        parts.append(created)
    return " | ".join(parts)


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_key(query: str, user_id: int, doc_ids_repr: str) -> str:
    """
    Build the MD5 cache key for a query.

    Including doc_ids_repr in the key means two users with different
    visibility scopes never share a cached answer.

    Args:
        query: Normalized query text.
        user_id: Requesting user's ID.
        doc_ids_repr: Sorted, comma-joined doc IDs (or "*" for unrestricted).

    Returns:
        Hex MD5 digest used as the `app_search_cache.cache_key`.
    """
    raw = f"{query}|{user_id}|{doc_ids_repr}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cache_get(key: str) -> Optional[Dict]:
    """
    Look up a non-expired cached pipeline result and bump its hit_count.

    Args:
        key: Cache key from `_cache_key`.

    Returns:
        The decoded result dict if a fresh (non-expired) entry exists,
        else None. Never raises — any SQL/JSON error is swallowed.
    """
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT results FROM app_search_cache
                WHERE cache_key = ? AND expires_at > GETUTCDATE()
            """, key)
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE app_search_cache SET hit_count = hit_count+1 WHERE cache_key = ?", key)
                conn.commit()
                return json.loads(row[0])
    except Exception:
        pass
    return None


class _DatetimeEncoder(json.JSONEncoder):
    """JSON encoder that serializes datetime-like objects via .isoformat()."""

    def default(self, obj):
        """Return an ISO-8601 string for datetime-like objects, else defer to the base encoder."""
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return super().default(obj)


def _cache_set(key: str, query: str, user_id: int, payload: Dict) -> None:
    """
    Write a pipeline result to `app_search_cache` (MERGE upsert), TTL'd by
    `_CACHE_TTL_SECONDS`.

    Args:
        key: Cache key from `_cache_key`.
        query: Original query text (stored for inspection/debugging).
        user_id: Requesting user's ID.
        payload: The full pipeline output dict to cache.

    Never raises — failures are silently swallowed so caching issues never
    break retrieval.
    """
    try:
        from app_db import get_app_db
        expires = (datetime.utcnow() + timedelta(seconds=_CACHE_TTL_SECONDS)).isoformat()
        payload_str = json.dumps(payload, cls=_DatetimeEncoder)
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                MERGE app_search_cache AS t
                USING (SELECT ? AS cache_key) AS s ON t.cache_key = s.cache_key
                WHEN MATCHED THEN UPDATE SET results=?, expires_at=?, hit_count=1
                WHEN NOT MATCHED THEN INSERT (cache_key, query, user_id, results, expires_at)
                    VALUES (?, ?, ?, ?, ?)
            """, key, payload_str, expires,
                key, query, user_id, payload_str, expires)
            conn.commit()
    except Exception:
        pass


# ── Retrieval trace ───────────────────────────────────────────────────────────

def _trace(session_id: str, user_id: int, query: str,
           results: List[Dict], confidence: float,
           fallback: bool, method: str, latency_ms: int) -> None:
    """
    Best-effort insert of one retrieval observability row into
    `app_retrieval_traces`.

    Args:
        session_id: Chat/session identifier (may be empty).
        user_id: Requesting user's ID.
        query: Normalized query text.
        results: Top-N ranked chunks for this query (only doc_ids and the
            top 5 scores are persisted).
        confidence: Max confidence across the returned chunks.
        fallback: Whether the hallucination guard triggered.
        method: Retrieval method label (e.g. "hybrid_ann").
        latency_ms: End-to-end pipeline latency in milliseconds.

    Never raises — a tracing failure must never fail the request.
    """
    try:
        from app_db import get_app_db
        doc_ids   = list({r.get("document_id") for r in results if r.get("document_id")})
        top_scores = [round(r.get("score", 0), 4) for r in results[:5]]
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO app_retrieval_traces
                    (session_id, user_id, query, doc_ids, top_scores,
                     method, result_count, confidence, fallback, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, session_id, user_id, query,
                json.dumps(doc_ids), json.dumps(top_scores),
                method, len(results), confidence,
                1 if fallback else 0, latency_ms)
            conn.commit()
    except Exception:
        pass


# ── Session memory — SQL-persisted, in-process L1 cache ──────────────────────
# L1 cache avoids a DB hit on every single turn within the same process/request.
# SQL persistence means sessions survive restarts.

_session_store: Dict[str, List[Dict]] = {}   # L1 in-process cache


def _get_session_memory(session_id: str) -> List[Dict]:
    """
    Return the last `_SESSION_MEMORY_LEN` turns for a session.

    Checks the in-process L1 dict first; on a miss, loads from the
    SQL-persisted `app_rag_sessions` table (L2) and populates L1.

    Args:
        session_id: Chat/session identifier. Empty string returns [].

    Returns:
        List of {"query": str, "context": str} turn dicts, oldest first.
    """
    if not session_id:
        return []
    # L1 hit
    if session_id in _session_store:
        return _session_store[session_id][-_SESSION_MEMORY_LEN:]
    # L2: load from SQL
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT turns_json FROM app_rag_sessions WHERE session_id = ?",
                session_id)
            row = cur.fetchone()
        if row:
            turns = json.loads(row[0])
            _session_store[session_id] = turns
            return turns[-_SESSION_MEMORY_LEN:]
    except Exception:
        pass
    return []


def _update_session_memory(session_id: str, turn: Dict) -> None:
    """
    Append a turn to a session's memory (L1 dict) and persist the trimmed
    tail to SQL (L2, best-effort).

    Args:
        session_id: Chat/session identifier. No-ops if empty.
        turn: {"query": str, "context": str} dict to append.
    """
    if not session_id:
        return
    mem = _session_store.setdefault(session_id, [])
    mem.append(turn)
    if len(mem) > _SESSION_MEMORY_LEN * 2:
        mem = mem[-_SESSION_MEMORY_LEN:]
        _session_store[session_id] = mem
    # Persist to SQL (best-effort)
    try:
        from app_db import get_app_db
        payload = json.dumps(mem[-_SESSION_MEMORY_LEN:])
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                MERGE app_rag_sessions AS t
                USING (SELECT ? AS session_id) AS s ON t.session_id = s.session_id
                WHEN MATCHED THEN
                    UPDATE SET turns_json = ?, updated_at = GETUTCDATE()
                WHEN NOT MATCHED THEN
                    INSERT (session_id, turns_json) VALUES (?, ?)
            """, session_id, payload, session_id, payload)
            conn.commit()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

class RAGPipeline:
    """
    Enterprise RAG pipeline.

    run() returns:
    {
        "context":    str,           # formatted context block for LLM
        "citations":  [str, ...],    # citation strings, one per source used
        "confidence": float,         # max confidence across returned chunks
        "fallback":   bool,          # True → model should say "Insufficient Evidence"
        "results":    [...],         # raw ranked chunk dicts
        "session_context": str,      # prior-turn context for follow-up awareness
    }
    """

    def __init__(self,
                 hybrid_alpha: float = _HYBRID_ALPHA,
                 confidence_threshold: float = _CONFIDENCE_THRESHOLD,
                 use_reranker: bool = True,
                 use_cache: bool = True):
        """
        Args:
            hybrid_alpha: Semantic-vs-BM25 weight in the hybrid score merge
                (1.0 = pure vector, 0.0 = pure keyword). Defaults to
                RAG_HYBRID_ALPHA.
            confidence_threshold: Minimum max-confidence below which the
                hallucination guard triggers a fallback. Defaults to
                RAG_CONFIDENCE_THRESHOLD.
            use_reranker: Whether to run the cross-encoder rerank step.
            use_cache: Whether to read/write the query result cache.
        """
        self.alpha      = hybrid_alpha
        self.threshold  = confidence_threshold
        self.use_rerank = use_reranker
        self.use_cache  = use_cache

    # ── Main entry point ───────────────────────────────────────────────────────

    def run(self, query: str,
            doc_ids:        Optional[Set[str]] = None,
            pinned_doc_ids: Optional[Set[str]] = None,
            n_results:      int = 8,
            user_id:        int = 0,
            session_id:     str = "",
            scoped:         bool = False) -> Dict:
        """
        Run the full pipeline for a query.

        Retrieval order (correct enterprise pattern):
          1. ANN vector search  → top _RERANK_TOP_K candidates   (fast HNSW index)
          2. BM25 keyword score → on candidates only, not full table
          3. Hybrid merge       → weighted combination of both scores
          4. Cross-encoder rerank → deep scoring on merged candidates

        Args:
            query: Natural-language question. Whitespace-normalized;
                an empty/blank query short-circuits to an empty result.
            doc_ids: Restrict retrieval to this set of document IDs
                (access-controlled by the caller, e.g. via
                get_visible_doc_ids). None means no restriction.
            pinned_doc_ids: Documents the user explicitly attached/pinned —
                if any top chunk belongs to one of these, the hallucination
                guard is bypassed (fallback forced False) regardless of score.
            n_results: Number of top chunks to keep after reranking.
            user_id: Requesting user's ID (used in the cache key and trace).
            session_id: Chat/session identifier for session-memory
                continuity across turns; empty string disables it.
            scoped: Set True only for Stage 2 deep searches where the caller has
                already narrowed doc_ids to a small shortlist via filter_doc_ids.
                Enables per-document top-K excerpts and a much larger context
                budget. Must NOT be set for regular connector searches where
                doc_ids contains the full connector corpus.

        Returns:
            Dict with keys: context, citations, confidence, consistency,
            fallback, results, session_context, from_cache, latency_ms.
        """
        t0    = time.time()
        query = " ".join(query.strip().split())
        if not query:
            return self._empty(session_id)

        # ── Cache check ───────────────────────────────────────────────────────
        pinned       = pinned_doc_ids or set()
        doc_ids_repr = ",".join(sorted(doc_ids)) if doc_ids else "*"
        ckey         = _cache_key(query, user_id, doc_ids_repr)
        if self.use_cache:
            cached = _cache_get(ckey)
            if cached:
                cached["from_cache"] = True
                return cached

        # ── Step 2a: ANN vector search — uses HNSW index, no full table scan ─
        from agents.core.knowledge.vector_store import get_vector_store
        vs = get_vector_store()

        # When doc_ids explicitly scopes the search (e.g. Stage 2 deep search
        # after a Stage 1 shortlist via list_connector_documents), the caller
        # already narrowed the corpus — fetch and SCORE every chunk belonging
        # to those documents instead of capping at _RERANK_TOP_K. This is
        # completeness for scoring only; what actually gets sent to the LLM
        # is still filtered per-document below (see _per_document_top_k) —
        # otherwise this would just dump full resume text for everyone,
        # which isn't RAG, it's a file dump.
        if scoped:
            fetch_limit = max(_RERANK_TOP_K, _total_chunk_count(doc_ids))
        else:
            fetch_limit = _RERANK_TOP_K

        ann_hits = vs.search_documents(query, n_results=fetch_limit, doc_ids=doc_ids)
        if not ann_hits:
            return self._empty(session_id, fallback=True)

        # ── Step 2b: BM25 on ANN candidates only (not the full corpus) ────────
        texts     = [c["text"] for c in ann_hits]
        bm25_raw  = _bm25_scores(query, texts)
        bm25_max  = max(bm25_raw) if bm25_raw else 1.0
        bm25_norm = [s / bm25_max if bm25_max else 0.0 for s in bm25_raw]

        # ── Step 2c: Hybrid score merge ───────────────────────────────────────
        for i, chunk in enumerate(ann_hits):
            vec  = chunk.get("score", 0.0)
            bm25 = bm25_norm[i]
            chunk["score"] = round(self.alpha * vec + (1 - self.alpha) * bm25, 4)

        ann_hits.sort(key=lambda x: x["score"], reverse=True)
        candidates = ann_hits[:fetch_limit]

        # ── Step 3: Cross-encoder rerank ──────────────────────────────────────
        if self.use_rerank:
            candidates = _rerank(query, candidates)

        if scoped:
            # Every scoped document gets its own best-matching excerpt(s) —
            # not its full text, and not squeezed out by a single global
            # top-N that could let a few similar/verbose documents crowd out
            # the rest of the shortlist Stage 1 already decided was relevant.
            top_chunks = _per_document_top_k(candidates, _SCOPED_CHUNKS_PER_DOC)
        else:
            top_chunks = [_sanitize_chunk(c) for c in candidates[:n_results]]

        # ── Enrich with created_at — small targeted SQL lookup, not a full scan
        doc_id_list = list({c["document_id"] for c in top_chunks if c.get("document_id")})
        created_map = self._fetch_created_at(doc_id_list)
        for chunk in top_chunks:
            raw = created_map.get(chunk.get("document_id"))
            if raw is not None:
                # Always store as ISO string so JSON serialization never fails
                chunk["created_at"] = raw.isoformat() if hasattr(raw, "isoformat") else str(raw)

        # ── Step 4: Source confidence scoring (includes cross-source consistency)
        consistency = _consistency_score(top_chunks)
        for chunk in top_chunks:
            chunk["confidence"] = _source_confidence(
                chunk, chunk.get("score", 0), consistency)
            chunk["consistency"] = consistency   # expose for observability

        # ── Step 6: Hallucination check ───────────────────────────────────────
        max_confidence = max((c.get("confidence", 0) for c in top_chunks), default=0)
        if pinned and any(c.get("document_id") in pinned for c in top_chunks):
            fallback = False   # user explicitly attached this document
        elif scoped:
            fallback = False   # user explicitly scoped to specific documents — trust the results
        else:
            fallback = max_confidence < self.threshold

        # ── Step 5: Build context block ───────────────────────────────────────
        context, citations = self._build_context(
            top_chunks, fallback,
            max_chars=_SCOPED_MAX_CONTEXT_CHARS if scoped else None)

        # ── Step 7: Session memory ────────────────────────────────────────────
        prior_context = self._session_context(session_id)
        _update_session_memory(session_id, {"query": query, "context": context[:500]})

        # ── Cache write + trace ───────────────────────────────────────────────
        latency_ms = int((time.time() - t0) * 1000)
        output = {
            "context":         context,
            "citations":       citations,
            "confidence":      round(max_confidence, 4),
            "consistency":     consistency,
            "fallback":        fallback,
            "results":         top_chunks,
            "session_context": prior_context,
            "from_cache":      False,
            "latency_ms":      latency_ms,
        }
        if self.use_cache:
            _cache_set(ckey, query, user_id, output)
        try:
            _trace(session_id, user_id, query, top_chunks,
                   max_confidence, fallback, "hybrid_ann", latency_ms)
        except Exception:
            pass
        return output

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_created_at(self, doc_ids: List[str]) -> Dict:
        """
        Batch-fetch created_at for a small list of doc IDs (top results only).

        Args:
            doc_ids: Document IDs to look up.

        Returns:
            {document_id: created_at} dict; empty dict on no IDs or SQL error.
        """
        if not doc_ids:
            return {}
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                ph  = ",".join("?" for _ in doc_ids)
                cur.execute(
                    f"SELECT id, created_at FROM app_documents WHERE id IN ({ph})",
                    *doc_ids)
                return {r[0]: r[1] for r in cur.fetchall()}
        except Exception:
            return {}

    def _build_context(self, chunks: List[Dict], fallback: bool,
                       max_chars: Optional[int] = None):
        """
        Build the LLM-facing context block and parallel citation list.

        Args:
            chunks: Top-N ranked, confidence-scored result chunks.
            fallback: If True (or chunks is empty), returns the fixed
                "insufficient evidence" instruction block instead, with no
                citations.
            max_chars: Override for the context size cap. None uses the
                default RAG_MAX_CONTEXT_CHARS (unscoped search); pass
                _SCOPED_MAX_CONTEXT_CHARS for doc_ids-scoped deep searches
                where completeness matters more than a tight context budget.

        Returns:
            (context, citations) tuple — context is the formatted text
            block (capped at max_chars or RAG_MAX_CONTEXT_CHARS), citations
            is a list of "[N] filename | chunk K | date" strings, one per
            source used.
        """
        if fallback or not chunks:
            return (
                "⚠️ INSUFFICIENT EVIDENCE: No sufficiently relevant documents were found "
                "in the knowledge base to answer this query. Do NOT make assumptions or "
                "use external knowledge. Respond with: "
                "\"I don't have enough information in the available documents to answer this question.\"",
                [],
            )

        limit = max_chars if max_chars is not None else _MAX_CONTEXT_CHARS
        ctx_parts  = []
        citations  = []
        total_chars = 0

        for i, chunk in enumerate(chunks, 1):
            text = chunk.get("text_content") or chunk.get("text", "")
            if not text:
                continue
            if total_chars + len(text) > limit:
                text = text[: limit - total_chars]

            conf = chunk.get("confidence", chunk.get("score", 0))
            citation = _build_citation(chunk, i)
            citations.append(citation)

            ctx_parts.append(
                f"[Source {i}: {chunk.get('filename') or chunk.get('source_name','Unknown')} "
                f"| Confidence: {conf:.2f}]\n{text}"
            )
            total_chars += len(text)
            if total_chars >= limit:
                break

        header = (
            "=== RETRIEVED DOCUMENT CONTEXT ===\n"
            "Instructions: Answer ONLY using the context below. "
            "Do NOT use external knowledge. Cite sources as [Source N] inline. "
            "If the context doesn't contain enough information say so explicitly.\n\n"
        )
        context = header + "\n\n---\n\n".join(ctx_parts)
        return context, citations

    def _session_context(self, session_id: str) -> str:
        """
        Format prior-turn session memory as a prependable context block.

        Args:
            session_id: Chat/session identifier. Empty string returns "".

        Returns:
            A "=== PRIOR CONVERSATION CONTEXT ===" block listing each prior
            turn's query and context excerpt, or "" if there is no memory.
        """
        if not session_id:
            return ""
        turns = _get_session_memory(session_id)
        if not turns:
            return ""
        parts = [f"Q: {t['query']}\nContext excerpt: {t['context']}" for t in turns]
        return "=== PRIOR CONVERSATION CONTEXT ===\n" + "\n\n".join(parts)

    def _empty(self, session_id: str = "", fallback: bool = False) -> Dict:
        """
        Build the zero-result response shape (blank query, or no ANN hits).

        Args:
            session_id: Chat/session identifier, used to still surface prior
                turn context even when this turn returned nothing.
            fallback: If True, includes the "insufficient evidence" message;
                if False (blank-query case), context is "".

        Returns:
            Dict matching run()'s return shape with empty results/citations.
        """
        msg = (
            "⚠️ INSUFFICIENT EVIDENCE: The knowledge base is empty or no documents "
            "are accessible for your query."
            if fallback else ""
        )
        return {
            "context":         msg,
            "citations":       [],
            "confidence":      0.0,
            "fallback":        fallback,
            "results":         [],
            "session_context": self._session_context(session_id),
            "from_cache":      False,
            "latency_ms":      0,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

_pipeline: Optional[RAGPipeline] = None


def get_pipeline() -> RAGPipeline:
    """Return the process-wide singleton RAGPipeline, creating it on first call."""
    global _pipeline
    if _pipeline is None:
        _pipeline = RAGPipeline()
    return _pipeline


# ═══════════════════════════════════════════════════════════════════════════════
# CONTINUOUS EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def add_eval_query(query: str, expected_doc_ids: Optional[List[str]] = None,
                   category: str = "recall") -> int:
    """
    Register a test query in the evaluation suite.

    Args:
        query: The natural-language test query.
        expected_doc_ids: Document IDs considered a correct retrieval for
            this query, used to compute Recall@K in `run_evaluation`.
        category: Free-form label for grouping queries (default "recall").

    Returns:
        The new row's ID, or -1 if the insert returned no row.
    """
    from app_db import get_app_db
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_rag_eval_queries (query, expected_doc_ids, category)
            OUTPUT INSERTED.id
            VALUES (?, ?, ?)
        """, query,
            json.dumps(expected_doc_ids or []),
            category)
        row = cur.fetchone()
        conn.commit()
    return row[0] if row else -1


def list_eval_queries() -> List[Dict]:
    """Return all registered eval queries, newest first, with created_at stringified."""
    from app_db import get_app_db
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, query, expected_doc_ids, category, created_at
            FROM app_rag_eval_queries ORDER BY created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows


def delete_eval_query(query_id: int) -> bool:
    """Delete a registered eval query by ID. Returns True on success, False on error."""
    from app_db import get_app_db
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM app_rag_eval_queries WHERE id = ?", query_id)
            conn.commit()
        return True
    except Exception:
        return False


def run_evaluation(user_id: int = 0,
                   doc_ids: Optional[Set[str]] = None) -> Dict:
    """
    Run all registered eval queries through the RAG pipeline.
    Computes Recall@1, Recall@3, Recall@5 for each query and stores results.

    Recall@K = 1 if any expected doc appears in top-K results, else 0.
    Returns summary metrics.
    """
    from app_db import get_app_db

    queries = list_eval_queries()
    if not queries:
        return {"success": False, "error": "No eval queries registered"}

    pipeline = RAGPipeline(use_cache=False, use_reranker=True)
    summary = {"total": len(queries), "recall_at_1": [], "recall_at_3": [],
               "recall_at_5": [], "fallback_count": 0, "avg_latency_ms": []}

    for eq in queries:
        expected = set(json.loads(eq.get("expected_doc_ids") or "[]"))
        out = pipeline.run(eq["query"], doc_ids=doc_ids, user_id=user_id,
                           n_results=5)

        actual_ids = [r.get("document_id") for r in out.get("results", [])]
        r1 = 1.0 if any(d in expected for d in actual_ids[:1]) else 0.0
        r3 = 1.0 if any(d in expected for d in actual_ids[:3]) else 0.0
        r5 = 1.0 if any(d in expected for d in actual_ids[:5]) else 0.0

        summary["recall_at_1"].append(r1)
        summary["recall_at_3"].append(r3)
        summary["recall_at_5"].append(r5)
        summary["avg_latency_ms"].append(out.get("latency_ms", 0))
        if out.get("fallback"):
            summary["fallback_count"] += 1

        # Persist result
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO app_rag_eval_results
                        (eval_query_id, recall_at_1, recall_at_3, recall_at_5,
                         confidence, consistency, fallback, latency_ms, actual_doc_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, eq["id"], r1, r3, r5,
                    out.get("confidence", 0),
                    out.get("consistency", 0),
                    1 if out.get("fallback") else 0,
                    out.get("latency_ms", 0),
                    json.dumps(actual_ids))
                conn.commit()
        except Exception as exc:
            logger.warning("run_evaluation: failed to store result: %s", exc)

    n = len(queries)
    return {
        "success":       True,
        "total_queries": n,
        "recall_at_1":   round(sum(summary["recall_at_1"]) / n, 4),
        "recall_at_3":   round(sum(summary["recall_at_3"]) / n, 4),
        "recall_at_5":   round(sum(summary["recall_at_5"]) / n, 4),
        "fallback_rate": round(summary["fallback_count"] / n, 4),
        "avg_latency_ms": round(sum(summary["avg_latency_ms"]) / n, 1),
    }


def get_eval_results(limit: int = 50) -> List[Dict]:
    """
    Return the most recent eval run results, joined with their query text/category.

    Args:
        limit: Maximum number of rows to return (newest first).

    Returns:
        List of result dicts with run_at stringified and fallback cast to bool.
    """
    from app_db import get_app_db
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT TOP ({limit})
                   r.id, r.eval_query_id, q.query, q.category,
                   r.recall_at_1, r.recall_at_3, r.recall_at_5,
                   r.confidence, r.consistency, r.fallback,
                   r.latency_ms, r.actual_doc_ids, r.run_at
            FROM app_rag_eval_results r
            JOIN app_rag_eval_queries q ON q.id = r.eval_query_id
            ORDER BY r.run_at DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        if r.get("run_at"):
            r["run_at"] = str(r["run_at"])
        r["fallback"] = bool(r.get("fallback"))
    return rows


def get_rag_stats(days: int = 7) -> Dict:
    """Aggregate metrics from app_retrieval_traces for the last N days."""
    from app_db import get_app_db
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    COUNT(*)                                         AS total_queries,
                    AVG(CAST(latency_ms AS FLOAT))                   AS avg_latency_ms,
                    AVG(CAST(confidence AS FLOAT))                   AS avg_confidence,
                    SUM(CAST(fallback AS INT))                       AS fallback_count,
                    AVG(CAST(result_count AS FLOAT))                 AS avg_result_count,
                    MAX(created_at)                                  AS last_query_at,
                    SUM(CASE WHEN fallback = 1 THEN 1 ELSE 0 END) * 1.0
                        / NULLIF(COUNT(*), 0)                        AS fallback_rate
                FROM app_retrieval_traces
                WHERE created_at >= DATEADD(day, ?, GETUTCDATE())
            """, -days)
            cols  = [d[0] for d in cur.description]
            stats = dict(zip(cols, cur.fetchone()))

            # Top 5 most-searched queries
            cur.execute("""
                SELECT TOP 5 query, COUNT(*) AS times
                FROM app_retrieval_traces
                WHERE created_at >= DATEADD(day, ?, GETUTCDATE())
                GROUP BY query ORDER BY times DESC
            """, -days)
            top_queries = [{"query": r[0], "times": r[1]} for r in cur.fetchall()]

            # Latency buckets
            cur.execute("""
                SELECT
                    SUM(CASE WHEN latency_ms < 500  THEN 1 ELSE 0 END) AS fast,
                    SUM(CASE WHEN latency_ms BETWEEN 500 AND 2000 THEN 1 ELSE 0 END) AS medium,
                    SUM(CASE WHEN latency_ms > 2000 THEN 1 ELSE 0 END) AS slow
                FROM app_retrieval_traces
                WHERE created_at >= DATEADD(day, ?, GETUTCDATE())
            """, -days)
            r = cur.fetchone()
            latency_buckets = {"fast_<500ms": r[0], "medium_500-2000ms": r[1], "slow_>2000ms": r[2]}

        if stats.get("last_query_at"):
            stats["last_query_at"] = str(stats["last_query_at"])
        for k, v in stats.items():
            if v is not None and not isinstance(v, str):
                stats[k] = round(float(v), 4) if isinstance(v, float) else v

        return {
            "period_days":    days,
            "summary":        stats,
            "top_queries":    top_queries,
            "latency_buckets": latency_buckets,
        }
    except Exception as exc:
        logger.warning("get_rag_stats: %s", exc)
        return {"period_days": days, "error": str(exc)}
