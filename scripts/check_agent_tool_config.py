"""
check_agent_tool_config.py — Dump tools_json for hub agents that reference
list_connector_documents / search_connector_knowledge, to verify whether
connector_keys is actually configured for list_connector_documents.

Usage:
    python scripts/check_agent_tool_config.py
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

from app_db import get_app_db

with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, tools_json
        FROM hub_agents
        WHERE tools_json LIKE '%list_connector_documents%'
           OR tools_json LIKE '%search_connector_knowledge%'
    """)
    rows = cur.fetchall()

if not rows:
    print("No hub agents found referencing these tools.")
else:
    for agent_id, name, tools_json in rows:
        print(f"\n=== Agent: {name!r} (id={agent_id}) ===")
        try:
            tools = json.loads(tools_json or "[]")
        except Exception as exc:
            print(f"  FAILED TO PARSE tools_json: {exc}")
            print(f"  raw: {tools_json}")
            continue
        for t in tools:
            if isinstance(t, str):
                print(f"  - {t}  (plain string entry — NO config object at all)")
            elif isinstance(t, dict):
                tname = t.get("name")
                cfg = t.get("config")
                if tname in ("list_connector_documents", "search_connector_knowledge",
                             "list_knowledge_documents", "search_knowledge"):
                    print(f"  - {tname}: config = {json.dumps(cfg)}")
