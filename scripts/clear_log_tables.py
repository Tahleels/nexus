"""
clear_log_tables.py — Truncate connector_logs and app_retrieval_traces.

Both are pure observability log tables (database/app_db.py) with no
foreign keys pointing at them, so they can be cleared independently and
safely.

Usage:
    python scripts/clear_log_tables.py                 # clear both, asks to confirm
    python scripts/clear_log_tables.py --yes            # clear both, no prompt
    python scripts/clear_log_tables.py --connector-only # only connector_logs
    python scripts/clear_log_tables.py --traces-only    # only app_retrieval_traces
"""
import sys
import argparse
from pathlib import Path

_BASE = str(Path(__file__).resolve().parent.parent)
for _pkg in ("", "core", "database", "services", "generators", "nlq", "blueprints"):
    _p = str(Path(_BASE, _pkg)) if _pkg else _BASE
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(_BASE, ".env"))  # noqa: E402

from app_db import get_app_db  # noqa: E402

TABLES = {
    "connector": "connector_logs",
    "traces": "app_retrieval_traces",
}


def row_count(cursor, table):
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    return cursor.fetchone()[0]


def clear_table(cursor, table):
    cursor.execute(f"DELETE FROM {table}")
    return cursor.rowcount


def main():
    parser = argparse.ArgumentParser(description="Clear connector/retrieval-trace log tables.")
    parser.add_argument("--connector-only", action="store_true", help="Only clear connector_logs.")
    parser.add_argument("--traces-only", action="store_true", help="Only clear app_retrieval_traces.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    if args.connector_only:
        targets = [TABLES["connector"]]
    elif args.traces_only:
        targets = [TABLES["traces"]]
    else:
        targets = list(TABLES.values())

    with get_app_db() as conn:
        cursor = conn.cursor()

        counts = {t: row_count(cursor, t) for t in targets}
        print("Current row counts:")
        for t, c in counts.items():
            print(f"  {t}: {c}")

        if all(c == 0 for c in counts.values()):
            print("Nothing to delete.")
            return

        if not args.yes:
            confirm = input(f"\nDelete all rows from {', '.join(targets)}? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted.")
                return

        for t in targets:
            deleted = clear_table(cursor, t)
            print(f"Deleted {deleted} row(s) from {t}.")

        conn.commit()


if __name__ == "__main__":
    main()
