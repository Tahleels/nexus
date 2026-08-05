"""
backfill_document_profiles.py — One-time backfill for the document_profiles table.

Populates document_profiles for every document already ingested into
app_documents (e.g. the 54 SharePoint resumes), without re-processing or
re-downloading any files. Reads existing chunk text from the active vector
store backend (LanceDB by default — see agents/core/knowledge/vector_store.py),
generates a summary via GPT-4o-mini, and upserts a row into document_profiles
(creating the table first if it doesn't exist).

Re-runs overwrite existing profile rows (MERGE upsert), so it's safe to
re-run after fixing the chunk source — useful if an earlier run produced
placeholder "No chunk text available" summaries.

Usage:
    python scripts/backfill_document_profiles.py
"""
import sys
import io
import json
import os
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.normpath(os.path.join(_HERE, ".."))
for _pkg in ("", "core", "database", "services", "generators", "nlq", "blueprints",
             "agents", "agents/core", "agents/core/knowledge"):
    _p = str(Path(_BASE, _pkg)) if _pkg else _BASE
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv
load_dotenv(Path(_BASE, ".env"))

from app_db import get_app_db, ensure_tables
from document_processor import _generate_summary, _upsert_document_profile
from vector_store import get_vector_store

ensure_tables()
print("Tables verified/created.")

vs = get_vector_store()
print(f"Vector store backend: {vs.stats().get('backend', 'unknown')}")

with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT d.id, d.filename, d.source_watch_id, d.source_watch_type, d.extra_meta
        FROM app_documents d
        ORDER BY d.created_at
    """)
    docs = cur.fetchall()

print(f"Found {len(docs)} documents to (re)process.")

ok_count, warn_count = 0, 0

for i, (doc_id, filename, watch_id, watch_type, extra_meta_str) in enumerate(docs, 1):
    print(f"[{i}/{len(docs)}] {filename}")

    # Pull this document's chunks from the vector store (works for LanceDB/Azure/SQL backends)
    try:
        hits = vs.search_documents(query=filename, n_results=50, doc_ids={doc_id})
    except Exception as exc:
        print(f"  ERROR fetching chunks: {exc}")
        hits = []

    if not hits:
        print(f"  WARN — no chunks found in vector store, using filename as summary")
        summary = f"Document: {filename}. No chunk text available."
        warn_count += 1
    else:
        # Sort by chunk_index so the summary reads in original document order
        hits.sort(key=lambda h: int(h.get("metadata", {}).get("chunk_index") or 0))
        combined = "\n\n".join(h["text"] for h in hits[:3] if h.get("text"))
        summary = _generate_summary(combined)
        ok_count += 1

    key_metrics = None
    try:
        meta = json.loads(extra_meta_str or "{}")
        km = meta.get("extracted_metrics")
        if km and isinstance(km, dict):
            key_metrics = km
    except Exception:
        pass

    connector_key = (f"{watch_type}:{watch_id}" if watch_id and watch_type else None)

    _upsert_document_profile(
        doc_id=doc_id,
        source_file=filename,
        summary=summary,
        key_metrics=key_metrics,
        source_type="connector" if connector_key else "knowledge",
        connector_key=connector_key,
        document_type="general",
    )
    print(f"  OK: {summary[:100]}...")

print(f"\n=== DONE — {ok_count} with real summaries, {warn_count} with no chunks found ===")
with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM document_profiles")
    print(f"document_profiles total: {cur.fetchone()[0]}")
    cur.execute("SELECT connector_key, COUNT(*) as cnt FROM document_profiles GROUP BY connector_key")
    for row in cur.fetchall():
        print(f"  {row[0] or '(no connector)'}: {row[1]} docs")
