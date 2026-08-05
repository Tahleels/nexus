"""One-off: drop the CSV→SQL import feature's tables now that the feature has been removed."""
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
for _pkg in ("core", "database", "services", "generators", "nlq", "blueprints"):
    sys.path.insert(0, os.path.join(_BASE, _pkg))

from dotenv import load_dotenv
load_dotenv(os.path.join(_BASE, ".env"))

from app_db import get_app_db

TABLES = ["csv_sql_import_logs", "csv_import_strategies", "csv_sql_imports"]

with get_app_db() as conn:
    cursor = conn.cursor()
    for t in TABLES:
        cursor.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?", t
        )
        exists = cursor.fetchone() is not None
        if exists:
            print(f"Dropping {t} ...")
            cursor.execute(f"DROP TABLE {t}")
        else:
            print(f"{t} does not exist, skipping")
    conn.commit()
print("Done.")
