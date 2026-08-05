"""
drop_ga_table.py — Drop [ga].[analytics] so the importer can recreate it
                    with correct column names and proper SQL types.

Run:  python drop_ga_table.py
"""

import os
import pyodbc

# ── Connection details — set these env vars, or edit directly for local runs ──
SERVER   = os.getenv("GA_DB_SERVER", "localhost")
PORT     = os.getenv("GA_DB_PORT", "1433")
DATABASE = os.getenv("GA_DB_DATABASE", "")   # the database where ga.analytics lives
USERNAME = os.getenv("GA_DB_USERNAME", "")
PASSWORD = os.getenv("GA_DB_PASSWORD", "")
# ─────────────────────────────────────────────────────────────────────────────

TABLE = "[ga].[analytics]"

conn_str = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SERVER},{PORT};"
    f"DATABASE={DATABASE};"
    f"UID={USERNAME};"
    f"PWD={PASSWORD};"
    f"TrustServerCertificate=yes;"
)

try:
    conn = pyodbc.connect(conn_str, autocommit=True)
    cur  = conn.cursor()

    cur.execute(f"""
        IF EXISTS (
            SELECT 1 FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = 'ga' AND TABLE_NAME = 'analytics'
        )
        DROP TABLE {TABLE}
    """)

    print(f"Done — {TABLE} dropped (or did not exist).")
    conn.close()

except Exception as e:
    print(f"Error: {e}")
