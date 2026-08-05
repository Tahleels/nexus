"""
backfill_summary_embeddings.py — One-time backfill: (re)embed summary + key
skills for every document_profiles row.

Embeds summary + key_metrics["Key Skills"] combined (not summary alone) —
the prose summary can under-represent specialized/rare skills (e.g. GenAI
experience on an otherwise generic Azure Data Engineer resume) relative to
more common background, so Key Skills is appended verbatim to make sure
specific technical terms directly influence the embedding.

Re-processes ALL rows (not just ones missing an embedding) so that if this
is being re-run after the embedding-text fix, stale embeddings computed
from summary-alone get corrected too. Safe to re-run any time.

Usage:
    python scripts/backfill_summary_embeddings.py
"""
import sys
import io
import json
import os
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.normpath(os.path.join(_HERE, ".."))
for _pkg in ("", "core", "database"):
    _p = str(Path(_BASE, _pkg)) if _pkg else _BASE
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv
load_dotenv(Path(_BASE, ".env"))

from app_db import get_app_db, ensure_tables
from agents.core.knowledge.document_processor import _embed_summary, _build_embedding_text

ensure_tables()
print("Tables verified/created.")

with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT doc_id, summary, key_metrics
        FROM document_profiles
        WHERE summary IS NOT NULL
        ORDER BY processed_at
    """)
    rows = cur.fetchall()

print(f"Found {len(rows)} profiles to (re)embed with summary + key skills.")

ok, failed = 0, 0
for i, (doc_id, summary, key_metrics_json) in enumerate(rows, 1):
    key_metrics = None
    if key_metrics_json:
        try:
            key_metrics = json.loads(key_metrics_json)
        except Exception:
            pass

    embedding_text = _build_embedding_text(summary, key_metrics)
    emb = _embed_summary(embedding_text)
    if not emb:
        print(f"[{i}/{len(rows)}] {doc_id} — FAILED to embed")
        failed += 1
        continue
    with get_app_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE document_profiles SET summary_embedding = ? WHERE doc_id = ?",
            json.dumps(emb), doc_id)
        conn.commit()
    ok += 1
    if i % 10 == 0 or i == len(rows):
        print(f"[{i}/{len(rows)}] embedded so far: {ok}, failed: {failed}")

print(f"\n=== DONE — {ok} embedded, {failed} failed ===")

with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM document_profiles WHERE summary_embedding IS NOT NULL")
    print(f"document_profiles with summary_embedding: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM document_profiles")
    print(f"document_profiles total: {cur.fetchone()[0]}")
