"""
fix_hr_agent_tool_config.py — One-time fix: copy connector_keys config from
search_connector_knowledge into list_connector_documents for the HR Recruiter
hub agent, since the agent-editor UI saved it with config=null.

Usage:
    python scripts/fix_hr_agent_tool_config.py
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

AGENT_ID = "2c5c5b4a-6156-48f0-9518-1d71093981a7"  # HR Recruiter

with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("SELECT name, tools_json FROM hub_agents WHERE id = ?", AGENT_ID)
    row = cur.fetchone()
    if not row:
        print(f"Agent {AGENT_ID} not found.")
        sys.exit(1)

    name, tools_json = row
    tools = json.loads(tools_json or "[]")

    # Find the reference config from search_connector_knowledge
    ref_config = None
    for t in tools:
        if isinstance(t, dict) and t.get("name") == "search_connector_knowledge":
            ref_config = t.get("config")
            break

    if not ref_config or not ref_config.get("connector_keys"):
        print("ERROR: search_connector_knowledge has no connector_keys config to copy from.")
        sys.exit(1)

    print(f"Reference config from search_connector_knowledge: {ref_config}")

    changed = False
    for t in tools:
        if isinstance(t, dict) and t.get("name") == "list_connector_documents":
            old = t.get("config")
            t["config"] = {"connector_keys": ref_config["connector_keys"]}
            print(f"list_connector_documents config: {old} -> {t['config']}")
            changed = True

    if not changed:
        print("ERROR: list_connector_documents tool not found on this agent.")
        sys.exit(1)

    cur.execute("UPDATE hub_agents SET tools_json = ? WHERE id = ?",
                json.dumps(tools), AGENT_ID)
    conn.commit()
    print(f"\nUpdated agent {name!r} (id={AGENT_ID}).")

# Verify
with get_app_db() as conn:
    cur = conn.cursor()
    cur.execute("SELECT tools_json FROM hub_agents WHERE id = ?", AGENT_ID)
    tools = json.loads(cur.fetchone()[0])
    for t in tools:
        if isinstance(t, dict) and t.get("name") in (
                "list_connector_documents", "search_connector_knowledge"):
            print(f"VERIFIED — {t['name']}: config = {t.get('config')}")
