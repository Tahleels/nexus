"""
clear_token_data.py
-------------------
Run once to wipe all token analytics so you can test fresh recording.

  python clear_token_data.py

What it clears:
  - token_usage            all rows (the main per-call analytics table)
  - ws_model_usage         all rows (workspace per-call analytics)
  - hub_agents             total_tokens + total_runs reset to 0
  - hub_workflows          total_runs reset to 0

What it does NOT touch:
  - hub_messages / ws_messages / bi_messages  (conversation content)
  - users, sessions, agents config, workflows config
  - any schema or column definitions
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# Load .env before importing config so DB credentials are available
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config
import pyodbc

STEPS = [
    ("DELETE token_usage",        "DELETE FROM token_usage"),
    ("DELETE ws_model_usage",     "DELETE FROM ws_model_usage"),
    ("RESET hub_agents counters", "UPDATE hub_agents   SET total_tokens=0, total_runs=0"),
    ("RESET hub_workflows runs",  "UPDATE hub_workflows SET total_runs=0"),
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
    print("\nDone — all token analytics data cleared. Safe to start testing.")


if __name__ == "__main__":
    main()
