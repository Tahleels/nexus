"""
delete_all_bi_jobs.py — Hard-delete BI jobs directly from SQL Server.

Why this exists: services/job_manager.py's delete_job() runs a permission
check (_check_owner) before deleting. If that check fails it returns
False, "Permission denied" WITHOUT raising or deleting anything — which
looks "silent" if the caller doesn't print the returned message. This
script bypasses the app layer entirely and deletes straight from the
tables, so it works regardless of created_by/role mismatches or dangling
agent_name references (app_jobs.agent_name is a plain string column with
no FK to app_agents, so a removed agent never blocks deletion at the DB
level).

Usage:
    python scripts/delete_all_bi_jobs.py            # delete ALL jobs (asks to confirm)
    python scripts/delete_all_bi_jobs.py --yes       # delete ALL jobs, no prompt
    python scripts/delete_all_bi_jobs.py --job-id <id>   # delete a single job by id
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


def list_jobs(cursor):
    cursor.execute("SELECT id, name, agent_name FROM app_jobs")
    return cursor.fetchall()


def delete_jobs(job_ids):
    if not job_ids:
        print("No jobs to delete.")
        return

    with get_app_db() as conn:
        cursor = conn.cursor()

        placeholders = ",".join("?" for _ in job_ids)

        cursor.execute(
            f"DELETE FROM app_job_executions WHERE job_id IN ({placeholders})",
            job_ids,
        )
        exec_deleted = cursor.rowcount
        print(f"Deleted {exec_deleted} row(s) from app_job_executions.")

        cursor.execute(
            f"DELETE FROM app_jobs WHERE id IN ({placeholders})",
            job_ids,
        )
        jobs_deleted = cursor.rowcount
        print(f"Deleted {jobs_deleted} row(s) from app_jobs.")

        conn.commit()

        if jobs_deleted != len(job_ids):
            print(
                f"WARNING: expected to delete {len(job_ids)} job(s) but only "
                f"{jobs_deleted} matched. Some ids may not exist anymore."
            )


def main():
    parser = argparse.ArgumentParser(description="Delete BI jobs directly from the database.")
    parser.add_argument("--job-id", help="Delete only this job id instead of all jobs.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    with get_app_db() as conn:
        cursor = conn.cursor()
        rows = list_jobs(cursor)

    if args.job_id:
        rows = [r for r in rows if r.id == args.job_id]
        if not rows:
            print(f"Job id {args.job_id} not found in app_jobs.")
            return

    if not rows:
        print("app_jobs is already empty.")
        return

    print(f"Found {len(rows)} job(s):")
    for r in rows:
        print(f"  id={r.id}  name={r.name!r}  agent_name={r.agent_name!r}")

    if not args.yes:
        confirm = input(f"\nDelete {len(rows)} job(s) listed above? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    delete_jobs([r.id for r in rows])


if __name__ == "__main__":
    main()
