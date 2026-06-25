"""Two things:

1. Behavioral-parity tripwire for HubWorkflowEngine — a tiny deterministic
   workflow (trigger -> transform -> output, no DB/LLM/agent nodes) run
   through .execute(), asserting the exact NDJSON event sequence. This is
   what protects against subtle behavior drift while HubWorkflowEngine's
   ~1,100 lines get relocated to agents/core/hub/workflow_engine.py.

2. The SQL-injection fix in the db_write node's table/column/where-clause
   identifier handling: a legitimate config must still work unchanged, and
   a malicious one must now be rejected instead of executing.
"""
import json


def _make_engine(app_module):
    import sys
    hub_bp = sys.modules["agents_hub_bp"]
    return hub_bp.HubWorkflowEngine(
        api_key="unused-no-llm-nodes-in-this-test",
        user={"id": 1, "username": "_pytest_admin", "role": "admin"},
        run_id="pytest-run",
        workflow_id="pytest-wf",
        workflow_name="pytest-tiny",
    )


TINY_WORKFLOW = {
    "name": "pytest-tiny",
    "execution_mode": "sequential",
    "nodes": [
        {"id": "n1", "type": "trigger", "label": "Trigger"},
        {"id": "n2", "type": "transform", "label": "Transform",
         "expression": "hello {{trigger_input}}"},
        {"id": "n3", "type": "output", "label": "Output"},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n3"},
    ],
}


def test_tiny_workflow_event_sequence(app_module):
    engine = _make_engine(app_module)
    events = [json.loads(line) for line in engine.execute(TINY_WORKFLOW, "world")]

    types = [e["type"] for e in events]
    assert types == [
        "wf_start",
        "node_start", "node_complete",  # n1 trigger
        "node_start", "node_complete",  # n2 transform
        "node_start", "node_complete",  # n3 output
        "wf_complete",
    ]
    assert events[0]["nodes"] == 3
    final = events[-1]
    assert final["final_output"] == "hello world"


# ── SQL-injection fix (db_write node) ───────────────────────────────────────

def _sqlite_conn_cfg(tmp_path):
    """A throwaway sqlite DB the db_write node can target directly — avoids
    needing a second SQL Server connection just for this test."""
    import sqlite3
    db_path = str(tmp_path / "sqli_test.db")
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT, status TEXT)")
    con.execute("INSERT INTO widgets (id, name, status) VALUES (1, 'a', 'active')")
    con.commit()
    con.close()
    return {"name": "pytest-sqlite", "type": "sqlite", "database": db_path,
            "server": "", "username": "", "password": "", "port": ""}


def test_db_write_legit_update_still_works(app_module, monkeypatch, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)

    import database_manager
    monkeypatch.setattr(
        database_manager.DatabaseConnectionManager, "load_connections",
        lambda self: [cfg],
    )

    node = {
        "connection_name": "pytest-sqlite",
        "table_name": "widgets",
        "operation": "UPDATE",
        "field_mapping": [{"column": "status", "value": "inactive"}],
        "where_clause": "id = 1",
    }
    result = engine._exec_db_write(node, {"vars": {}, "outputs": {}, "last_output": ""})
    assert "error" not in result, result
    assert result["rows_affected"] == 1
    assert result["table"] == "widgets"


def test_db_write_rejects_malicious_table_name(app_module, monkeypatch, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)

    import database_manager
    monkeypatch.setattr(
        database_manager.DatabaseConnectionManager, "load_connections",
        lambda self: [cfg],
    )

    node = {
        "connection_name": "pytest-sqlite",
        "table_name": "widgets; DROP TABLE widgets--",
        "operation": "UPDATE",
        "field_mapping": [{"column": "status", "value": "inactive"}],
        "where_clause": "id = 1",
    }
    result = engine._exec_db_write(node, {"vars": {}, "outputs": {}, "last_output": ""})
    assert "error" in result


def test_db_write_rejects_malicious_where_clause(app_module, monkeypatch, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)

    import database_manager
    monkeypatch.setattr(
        database_manager.DatabaseConnectionManager, "load_connections",
        lambda self: [cfg],
    )

    node = {
        "connection_name": "pytest-sqlite",
        "table_name": "widgets",
        "operation": "UPDATE",
        "field_mapping": [{"column": "status", "value": "inactive"}],
        "where_clause": "1=1; DROP TABLE widgets--",
    }
    result = engine._exec_db_write(node, {"vars": {}, "outputs": {}, "last_output": ""})
    assert "error" in result


# ── Direct unit tests for the validator itself ──────────────────────────────
# The end-to-end tests above go through a real DB driver, which has its own
# incidental protections (e.g. sqlite3's Python binding refuses multi-
# statement strings) that can mask whether THIS validation logic is actually
# doing the rejecting. These test _validate_write_identifiers() directly so
# the signal is unambiguous regardless of driver quirks.

def test_validator_accepts_legit_identifiers(app_module, monkeypatch, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)
    import database_manager
    monkeypatch.setattr(
        database_manager.DatabaseConnectionManager, "load_connections",
        lambda self: [cfg],
    )
    err = engine._validate_write_identifiers(cfg, "widgets", ["status", "name"], "id = 1")
    assert err is None


def test_validator_rejects_malformed_table_name(app_module, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)
    err = engine._validate_write_identifiers(
        cfg, "widgets; DROP TABLE widgets--", ["status"], "id = 1")
    assert err is not None


def test_validator_rejects_unknown_table(app_module, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)
    err = engine._validate_write_identifiers(cfg, "nope_not_a_real_table", ["status"], "id = 1")
    assert err is not None


def test_validator_rejects_unknown_column(app_module, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)
    err = engine._validate_write_identifiers(
        cfg, "widgets", ["status=(SELECT group_concat(name) FROM sqlite_master)--"], "id = 1")
    assert err is not None


def test_validator_rejects_injected_where_identifier(app_module, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)
    err = engine._validate_write_identifiers(
        cfg, "widgets", ["status"], "id = 1 OR secret_column = 'x'")
    assert err is not None


def test_validator_allows_legit_boolean_where(app_module, tmp_path):
    """A tautology like 1=1 uses no bare identifiers, so it's out of scope
    for identifier-allowlisting (it's a values/business-logic concern, not
    an injection surface) — the validator should not false-positive on it."""
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)
    err = engine._validate_write_identifiers(cfg, "widgets", ["status"], "1=1")
    assert err is None


def test_db_write_rejects_unknown_column(app_module, monkeypatch, tmp_path):
    engine = _make_engine(app_module)
    cfg = _sqlite_conn_cfg(tmp_path)

    import database_manager
    monkeypatch.setattr(
        database_manager.DatabaseConnectionManager, "load_connections",
        lambda self: [cfg],
    )

    node = {
        "connection_name": "pytest-sqlite",
        "table_name": "widgets",
        "operation": "UPDATE",
        "field_mapping": [{"column": "status; DROP TABLE widgets--", "value": "inactive"}],
        "where_clause": "id = 1",
    }
    result = engine._exec_db_write(node, {"vars": {}, "outputs": {}, "last_output": ""})
    assert "error" in result
