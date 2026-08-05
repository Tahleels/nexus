"""
clear_jobs_data.py
------------------
Run once to wipe all BI jobs and their execution history.

  python clear_jobs_data.py

What it clears:
  - app_job_executions   all execution logs
  - resource_departments rows where resource_type = 'app_job'
  - resource_projects    rows where resource_type = 'app_job'
  - app_jobs             all job definitions

What it does NOT touch:
  - users, agents, workflows, hub tables
  - any schema or column definitions
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config
import pyodbc

STEPS = [
    ("DELETE app_job_executions",
     "DELETE FROM app_job_executions"),
    ("DELETE resource_departments (app_job)",
     "DELETE FROM resource_departments WHERE resource_type = 'app_job'"),
    ("DELETE resource_projects (app_job)",
     "DELETE FROM resource_projects WHERE resource_type = 'app_job'"),
    ("DELETE app_jobs",
     "DELETE FROM app_jobs"),
]


def main():
    conn_str = (
        f"DRIVER={{{config.DB_CONFIG['driver']}}};"
        f"SERVER={config.DB_CONFIG['server']},{config.DB_CONFIG['port']};"
        f"DATABASE={config.DB_CONFIG['database']};"
        f"UID={config.DB_CONFIG['username']};"
        f"PWD={config.DB_CONFIG['password']};"
        f"TrustServerCertificate=yes;"
    )

    try:
        conn = pyodbc.connect(conn_str, autocommit=False)
    except Exception as e:
        print(f"[ERROR] Could not connect to database: {e}")
        sys.exit(1)

    cursor = conn.cursor()
    errors = []

    for label, sql in STEPS:
        try:
            cursor.execute(sql)
            rows = cursor.rowcount
            print(f"  OK  {label}  ({rows} rows affected)")
        except Exception as e:
            print(f"  SKIP  {label} — {e}")
            errors.append(label)

    if errors:
        conn.rollback()
        print(f"\n[ROLLED BACK] {len(errors)} step(s) failed — nothing was changed.")
        sys.exit(1)

    conn.commit()
    conn.close()
    print("\nDone — all BI jobs and execution history cleared.")


if __name__ == "__main__":
    main()
