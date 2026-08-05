"""
cleanup_rag_data.py — Wipe all ingested RAG document data.

Deletes:
  SQL tables  → app_documents, app_document_chunks, document_profiles,
                app_retrieval_traces, app_rag_eval_queries, app_rag_eval_results
  LanceDB dir → Data/lancedb  (or LANCEDB_PATH env var)

Does NOT touch:
  - SharePoint / connector watch config  (app_sharepoint_watches, etc.)
  - Users, clients, agents, jobs, workflows
  - app_knowledge_store  (manually added knowledge — see flag below)
  - Any code or connection settings

Run:
  python scripts/cleanup_rag_data.py           # dry-run preview
  python scripts/cleanup_rag_data.py --confirm # actually delete
  python scripts/cleanup_rag_data.py --confirm --also-knowledge  # also wipes app_knowledge_store
"""

import argparse
import os
import shutil
import sys

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "database"))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "core"))

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from app_db import get_app_db

# ── LanceDB path (mirrors vector_store.py._resolve_path) ─────────────────────
_LANCEDB_PATH = (
    os.environ.get("LANCEDB_PATH", "").strip()
    or os.path.join(_PROJECT_ROOT, "Data", "lancedb")
)

# ── Tables to wipe (order matters — children before parents) ─────────────────
RAG_TABLES = [
    # evaluation data
    "app_rag_eval_results",
    "app_rag_eval_queries",
    # observability traces
    "app_retrieval_traces",
    # document profiles (AI summaries + embeddings used for Stage 1 ranking)
    "document_profiles",
    # SQL chunk store (legacy fallback — main chunks live in LanceDB)
    "app_document_chunks",
    # document metadata (source files, chunk counts, access control)
    "app_documents",
]

KNOWLEDGE_TABLE = "app_knowledge_store"   # only cleared with --also-knowledge

# ─────────────────────────────────────────────────────────────────────────────

def count_rows(conn, table: str) -> int:
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return -1   # table might not exist yet


def table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?",
        table,
    )
    return cur.fetchone()[0] > 0


def lancedb_size(path: str) -> str:
    if not os.path.isdir(path):
        return "not found"
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    mb = total / (1024 * 1024)
    return f"{mb:.1f} MB"


def main():
    parser = argparse.ArgumentParser(description="Wipe all ingested RAG document data")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete (without this flag it's a dry run)")
    parser.add_argument("--also-knowledge", action="store_true",
                        help="Also wipe app_knowledge_store (manually added knowledge items)")
    args = parser.parse_args()

    tables_to_wipe = list(RAG_TABLES)
    if args.also_knowledge:
        tables_to_wipe.insert(0, KNOWLEDGE_TABLE)

    dry = not args.confirm

    print()
    print("=" * 60)
    print("  RAG DATA CLEANUP" + ("  [DRY RUN — nothing will be deleted]" if dry else "  [LIVE RUN]"))
    print("=" * 60)

    # ── Preview SQL tables ────────────────────────────────────────────────────
    print("\nSQL tables to wipe:")
    with get_app_db() as conn:
        for tbl in tables_to_wipe:
            if not table_exists(conn, tbl):
                print(f"  {tbl:<35} ⚠  table does not exist yet")
            else:
                rows = count_rows(conn, tbl)
                label = f"{rows:,} rows" if rows >= 0 else "?"
                print(f"  {tbl:<35} {label}")

        print(f"\n  SKIPPING (connections / config — not touched):")
        for skip in ("app_sharepoint_watches", "app_users", "app_clients",
                     "app_agents", "app_jobs", "app_workflows"):
            exists = table_exists(conn, skip)
            print(f"  {skip:<35} {'(exists)' if exists else '(not found)'}")

    # ── Preview LanceDB ───────────────────────────────────────────────────────
    print(f"\nLanceDB directory:")
    print(f"  {_LANCEDB_PATH}")
    print(f"  Size: {lancedb_size(_LANCEDB_PATH)}")

    if not args.also_knowledge:
        print(f"\n  NOTE: app_knowledge_store is NOT wiped (manually added knowledge).")
        print(f"        Pass --also-knowledge to include it.")

    print()

    if dry:
        print("Dry run complete. Run with --confirm to actually delete.")
        print()
        return

    # ── Confirm ───────────────────────────────────────────────────────────────
    ans = input("Type YES to proceed with deletion: ").strip()
    if ans != "YES":
        print("Aborted.")
        return

    print()

    # ── Wipe SQL tables ───────────────────────────────────────────────────────
    with get_app_db() as conn:
        for tbl in tables_to_wipe:
            if not table_exists(conn, tbl):
                print(f"  SKIP  {tbl} (does not exist)")
                continue
            try:
                conn.cursor().execute(f"DELETE FROM {tbl}")
                conn.commit()
                print(f"  OK    {tbl}")
            except Exception as exc:
                print(f"  FAIL  {tbl}: {exc}")

    # ── Delete LanceDB directory ──────────────────────────────────────────────
    if os.path.isdir(_LANCEDB_PATH):
        try:
            shutil.rmtree(_LANCEDB_PATH)
            print(f"  OK    LanceDB directory deleted: {_LANCEDB_PATH}")
        except Exception as exc:
            print(f"  FAIL  LanceDB delete: {exc}")
    else:
        print(f"  SKIP  LanceDB directory not found: {_LANCEDB_PATH}")

    print()
    print("Cleanup complete.")
    print("Re-ingest documents from SharePoint to rebuild the knowledge base.")
    print()


if __name__ == "__main__":
    main()
