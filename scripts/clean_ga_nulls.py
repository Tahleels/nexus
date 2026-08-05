"""
clean_ga_nulls.py — Delete rows where PageTitle IS NULL from [ga].[analytics]

Run:  python clean_ga_nulls.py
"""

import os
import pyodbc

SERVER   = os.getenv("GA_DB_SERVER", "localhost")
PORT     = os.getenv("GA_DB_PORT", "1433")
DATABASE = os.getenv("GA_DB_DATABASE", "")
USERNAME = os.getenv("GA_DB_USERNAME", "")
PASSWORD = os.getenv("GA_DB_PASSWORD", "")

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

    cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE [PageTitle] IS NULL")
    count = cur.fetchone()[0]
    print(f"Found {count} rows with NULL PageTitle.")

    if count > 0:
        cur.execute(f"DELETE FROM {TABLE} WHERE [PageTitle] IS NULL")
        print(f"Deleted {count} rows.")
    else:
        print("Nothing to delete.")

    conn.close()

except Exception as e:
    print(f"Error: {e}")
