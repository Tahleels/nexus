"""
app_db.py — Shared application database layer.

Provides:
  get_app_db()     — pyodbc connection to the app SQL Server (same DB as auth.py)
  ensure_tables()  — CREATE TABLE IF NOT EXISTS for all app-internal tables
  migrate_from_json() — one-time seed from legacy JSON files (idempotent)

Call ensure_tables() once at startup (app.py already imports this module).

This is the foundational connection helper that every other ``database/*.py``
module in this codebase (auth.py, org_db.py, clients_db.py, nexus_sync_db.py,
plus modules outside this package such as workspace_db.py and
bi_conversations_db.py) calls get_app_db() from — there is exactly one
pyodbc connection pattern, defined once here, and reused everywhere via
``with get_app_db() as conn: ...`` context-manager blocks.

``_TABLE_STMTS`` is an idempotent, append-only list of
``IF NOT EXISTS ... CREATE TABLE`` / ``ALTER TABLE`` statements — the
project's de facto migration log. New columns/tables are added by appending
a new statement, never by editing an old one in place, so re-running
ensure_tables() against an already-provisioned database is always a no-op
for existing objects.
"""

import json
from logging_config import get_logger
import pyodbc
from datetime import datetime
from pathlib import Path
import config

logger = get_logger(__name__)


# ── connection ────────────────────────────────────────────────────────────────

def get_app_db() -> pyodbc.Connection:
    """Open a raw pyodbc connection to the app SQL Server database."""
    conn_str = (
        f"DRIVER={{{config.DB_CONFIG['driver']}}};"
        f"SERVER={config.DB_CONFIG['server']},{config.DB_CONFIG['port']};"
        f"DATABASE={config.DB_CONFIG['database']};"
        f"UID={config.DB_CONFIG['username']};"
        f"PWD={config.DB_CONFIG['password']};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, autocommit=False)


# ── table definitions ─────────────────────────────────────────────────────────

_TABLE_STMTS = [
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_database_connections')
    CREATE TABLE app_database_connections (
        id         INT IDENTITY(1,1) PRIMARY KEY,
        name       NVARCHAR(255) NOT NULL,
        type       NVARCHAR(50)  NOT NULL,
        server     NVARCHAR(255) NOT NULL DEFAULT '',
        port       NVARCHAR(20)  NOT NULL DEFAULT '',
        username   NVARCHAR(255) NOT NULL DEFAULT '',
        password   NVARCHAR(500) NOT NULL DEFAULT '',
        db_name    NVARCHAR(255) NOT NULL DEFAULT '',
        status     NVARCHAR(50)  NOT NULL DEFAULT 'disconnected',
        created_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_app_db_conn_name UNIQUE (name)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_agents')
    CREATE TABLE app_agents (
        id                  INT IDENTITY(1,1) PRIMARY KEY,
        name                NVARCHAR(255) NOT NULL,
        description         NVARCHAR(MAX),
        database_connection NVARCHAR(255),
        selected_tables     NVARCHAR(MAX),
        selected_columns    NVARCHAR(MAX),
        schema_context      NVARCHAR(MAX),
        created_at          DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_app_agents_name UNIQUE (name)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_jobs')
    CREATE TABLE app_jobs (
        id                   NVARCHAR(36)  NOT NULL PRIMARY KEY,
        name                 NVARCHAR(255) NOT NULL,
        description          NVARCHAR(MAX),
        agent_name           NVARCHAR(255) NOT NULL,
        nlq_prompt           NVARCHAR(MAX) NOT NULL,
        tools                NVARCHAR(MAX),
        schedule             NVARCHAR(MAX),
        delivery             NVARCHAR(MAX),
        enabled              BIT           NOT NULL DEFAULT 1,
        created_by           INT           NOT NULL DEFAULT 0,
        created_by_username  NVARCHAR(255) NOT NULL DEFAULT 'system',
        created_by_role      NVARCHAR(50)  NOT NULL DEFAULT 'user',
        created_at           DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        updated_at           DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        last_run             DATETIME2     NULL,
        next_run             DATETIME2     NULL,
        run_count            INT           NOT NULL DEFAULT 0,
        last_status          NVARCHAR(50)  NULL,
        CONSTRAINT uq_app_jobs_name UNIQUE (name)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_job_executions')
    CREATE TABLE app_job_executions (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        exec_id         NVARCHAR(36)  NOT NULL,
        job_id          NVARCHAR(36)  NOT NULL,
        job_name        NVARCHAR(255) NOT NULL,
        triggered_by    NVARCHAR(50)  NOT NULL DEFAULT 'scheduler',
        status          NVARCHAR(50)  NOT NULL DEFAULT 'running',
        started_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        finished_at     DATETIME2     NULL,
        duration_ms     INT           NULL,
        error           NVARCHAR(MAX) NULL,
        tools_completed NVARCHAR(MAX),
        row_count       INT           NOT NULL DEFAULT 0,
        artifacts       NVARCHAR(MAX),
        query_result    NVARCHAR(MAX),
        log_lines       NVARCHAR(MAX),
        CONSTRAINT uq_app_job_exec UNIQUE (job_id, exec_id)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_knowledge_store')
    CREATE TABLE app_knowledge_store (
        id         INT IDENTITY(1,1) PRIMARY KEY,
        key_name   NVARCHAR(255) NOT NULL,
        value      NVARCHAR(MAX) NOT NULL,
        updated_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_app_knowledge_key UNIQUE (key_name)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_nlq_learning')
    CREATE TABLE app_nlq_learning (
        id         INT IDENTITY(1,1) PRIMARY KEY,
        agent_name NVARCHAR(255) NOT NULL,
        data       NVARCHAR(MAX) NOT NULL,
        updated_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_app_nlq_learning_agent UNIQUE (agent_name)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_schema_embedding_cache')
    CREATE TABLE app_schema_embedding_cache (
        cache_key    CHAR(32)      NOT NULL PRIMARY KEY,
        text_content NVARCHAR(MAX) NOT NULL,
        embedding    NVARCHAR(MAX) NOT NULL,
        created_at   DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_documents')
    CREATE TABLE app_documents (
        id               NVARCHAR(64)  NOT NULL PRIMARY KEY,
        filename         NVARCHAR(500) NOT NULL,
        file_type        NVARCHAR(50)  NOT NULL DEFAULT '',
        client_id        NVARCHAR(255) NOT NULL DEFAULT '',
        source_name      NVARCHAR(500) NOT NULL DEFAULT '',
        chunk_count      INT           NOT NULL DEFAULT 0,
        file_hash        NVARCHAR(32)  NOT NULL DEFAULT '',
        extra_meta       NVARCHAR(MAX) NOT NULL DEFAULT '{}',
        uploaded_by_id   INT           NOT NULL DEFAULT 0,
        uploaded_by_name NVARCHAR(255) NOT NULL DEFAULT '',
        version          INT           NOT NULL DEFAULT 1,
        parent_doc_id    NVARCHAR(64)  NULL,
        status           NVARCHAR(50)  NOT NULL DEFAULT 'ready',
        created_at       DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_documents' AND COLUMN_NAME='uploaded_by_id')
    ALTER TABLE app_documents
        ADD uploaded_by_id   INT           NOT NULL DEFAULT 0,
            uploaded_by_name NVARCHAR(255) NOT NULL DEFAULT '',
            version          INT           NOT NULL DEFAULT 1,
            parent_doc_id    NVARCHAR(64)  NULL,
            status           NVARCHAR(50)  NOT NULL DEFAULT 'ready'
    """,
    # ── Track which connector ingested a document ─────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_documents' AND COLUMN_NAME='source_watch_id')
    ALTER TABLE app_documents
        ADD source_watch_id   INT          NULL,
            source_watch_type NVARCHAR(20) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_document_assignments')
    CREATE TABLE app_document_assignments (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        doc_id           NVARCHAR(64)  NOT NULL,
        user_id          INT           NOT NULL,
        user_name        NVARCHAR(255) NOT NULL DEFAULT '',
        assigned_by_id   INT           NOT NULL DEFAULT 0,
        assigned_by_name NVARCHAR(255) NOT NULL DEFAULT '',
        assigned_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_doc_user_assignment UNIQUE (doc_id, user_id)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_document_chunks')
    CREATE TABLE app_document_chunks (
        id          NVARCHAR(100) NOT NULL PRIMARY KEY,
        document_id NVARCHAR(64)  NOT NULL,
        chunk_index INT           NOT NULL DEFAULT 0,
        text_content NVARCHAR(MAX) NOT NULL,
        embedding   NVARCHAR(MAX) NOT NULL,
        filename    NVARCHAR(500) NOT NULL DEFAULT '',
        file_type   NVARCHAR(50)  NOT NULL DEFAULT '',
        client_id   NVARCHAR(255) NOT NULL DEFAULT '',
        source_name NVARCHAR(500) NOT NULL DEFAULT '',
        created_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        INDEX ix_doc_chunks_document_id NONCLUSTERED (document_id),
        INDEX ix_doc_chunks_client_id   NONCLUSTERED (client_id)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_knowledge_store' AND COLUMN_NAME='embedding')
    ALTER TABLE app_knowledge_store ADD embedding NVARCHAR(MAX) NULL
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_clients')
    CREATE TABLE app_clients (
        id              NVARCHAR(100) NOT NULL PRIMARY KEY,
        name            NVARCHAR(500) NOT NULL DEFAULT '',
        email_db_config NVARCHAR(MAX) NOT NULL DEFAULT '{}',
        all_our_services NVARCHAR(MAX) NOT NULL DEFAULT '[]',
        ppt_history     NVARCHAR(MAX) NOT NULL DEFAULT '[]',
        extra_data      NVARCHAR(MAX) NOT NULL DEFAULT '{}',
        created_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_schema_graph')
    CREATE TABLE app_schema_graph (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        agent_name  NVARCHAR(255) NOT NULL,
        table_a     NVARCHAR(255) NOT NULL,
        table_b     NVARCHAR(255) NOT NULL,
        join_key    NVARCHAR(255) NOT NULL DEFAULT '',
        ref_key     NVARCHAR(255) NOT NULL DEFAULT 'id',
        weight      FLOAT         NOT NULL DEFAULT 1.0,
        schema_hash NVARCHAR(64)  NOT NULL DEFAULT '',
        created_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_schema_graph_edge UNIQUE (agent_name, table_a, table_b, join_key)
    )
    """,
    # ── Org: departments ──────────────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='departments')
    CREATE TABLE departments (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        name        NVARCHAR(255) NOT NULL,
        description NVARCHAR(MAX) NULL,
        color       NVARCHAR(20)  NOT NULL DEFAULT '#6366f1',
        created_by  INT           NOT NULL DEFAULT 0,
        created_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_departments_name UNIQUE (name)
    )
    """,
    # ── Org: projects (independent from departments) ──────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='projects')
    CREATE TABLE projects (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        name        NVARCHAR(255) NOT NULL,
        description NVARCHAR(MAX) NULL,
        color       NVARCHAR(20)  NOT NULL DEFAULT '#0ea5e9',
        created_by  INT           NOT NULL DEFAULT 0,
        created_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    # ── Org: user_departments ─────────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='user_departments')
    CREATE TABLE user_departments (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        user_id     INT       NOT NULL,
        dept_id     INT       NOT NULL,
        assigned_by INT       NOT NULL DEFAULT 0,
        assigned_at DATETIME2 NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_user_dept UNIQUE (user_id, dept_id)
    )
    """,
    # ── Org: user_projects ────────────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='user_projects')
    CREATE TABLE user_projects (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        user_id     INT       NOT NULL,
        project_id  INT       NOT NULL,
        assigned_by INT       NOT NULL DEFAULT 0,
        assigned_at DATETIME2 NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_user_project UNIQUE (user_id, project_id)
    )
    """,
    # ── Org columns on app_agents ─────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_agents' AND COLUMN_NAME='department_id')
    ALTER TABLE app_agents ADD department_id INT NULL, project_id INT NULL
    """,
    # ── Org columns on app_jobs ───────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_jobs' AND COLUMN_NAME='department_id')
    ALTER TABLE app_jobs ADD department_id INT NULL, project_id INT NULL
    """,
    # ── Org columns on hub_agents (table may not exist yet — wrapped in IF EXISTS) ──
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_agents')
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_NAME='hub_agents' AND COLUMN_NAME='department_id')
            ALTER TABLE hub_agents ADD department_id INT NULL, project_id INT NULL
    END
    """,
    # ── Org columns on hub_workflows ──────────────────────────────────────────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_workflows')
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_NAME='hub_workflows' AND COLUMN_NAME='department_id')
            ALTER TABLE hub_workflows ADD department_id INT NULL, project_id INT NULL
    END
    """,
    # ── Org columns on hub_jobs ───────────────────────────────────────────────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_jobs')
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_NAME='hub_jobs' AND COLUMN_NAME='department_id')
            ALTER TABLE hub_jobs ADD department_id INT NULL, project_id INT NULL
    END
    """,
    # ── Role column on user_departments ──────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='user_departments' AND COLUMN_NAME='role')
    ALTER TABLE user_departments ADD role NVARCHAR(50) NOT NULL DEFAULT 'member'
    """,
    # ── Role column on user_projects ──────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='user_projects' AND COLUMN_NAME='role')
    ALTER TABLE user_projects ADD role NVARCHAR(50) NOT NULL DEFAULT 'member'
    """,
    # ── Source column on user_departments ('manual' | 'portal') ──────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='user_departments' AND COLUMN_NAME='source')
    ALTER TABLE user_departments ADD source NVARCHAR(20) NOT NULL DEFAULT 'manual'
    """,
    # ── Source column on user_projects ('manual' | 'portal') ─────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='user_projects' AND COLUMN_NAME='source')
    ALTER TABLE user_projects ADD source NVARCHAR(20) NOT NULL DEFAULT 'manual'
    """,
    # ── R&D department seed — Workspace nav/access is gated on this department
    # existing (see sidebar.html + workspace_bp.py). Seeded with zero members;
    # an admin assigns users via Admin > Departments & Projects.
    """
    IF NOT EXISTS (SELECT 1 FROM departments WHERE name = 'R&D')
    INSERT INTO departments (name, description, color, created_by)
    VALUES ('R&D', 'AI Workspace — company R&D collaboration space', '#ef4444', 0)
    """,
    # ── Agent guardrails (per-user, per-agent data access rules) ─────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='agent_guardrails')
    CREATE TABLE agent_guardrails (
        id                 INT IDENTITY(1,1) PRIMARY KEY,
        user_id            INT           NOT NULL,
        agent_id           NVARCHAR(255) NOT NULL,
        agent_type         NVARCHAR(10)  NOT NULL DEFAULT 'hub',
        filter_rules       NVARCHAR(MAX) NULL,
        restrict_tables    NVARCHAR(MAX) NULL,
        custom_instruction NVARCHAR(MAX) NULL,
        created_by         INT           NOT NULL DEFAULT 0,
        created_at         DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        updated_at         DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_agent_guardrail UNIQUE (user_id, agent_id, agent_type)
    )
    """,
    # ── Guardrail scoping: extend agent_guardrails to support department/project scopes ──
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='agent_guardrails' AND COLUMN_NAME='scope_type')
    ALTER TABLE agent_guardrails ADD scope_type NVARCHAR(20) NOT NULL DEFAULT 'user'
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='agent_guardrails' AND COLUMN_NAME='scope_id')
    ALTER TABLE agent_guardrails ADD scope_id INT NULL
    """,
    """
    UPDATE agent_guardrails SET scope_id = user_id WHERE scope_id IS NULL
    """,
    """
    IF EXISTS (SELECT 1 FROM sys.key_constraints WHERE name='uq_agent_guardrail')
    ALTER TABLE agent_guardrails DROP CONSTRAINT uq_agent_guardrail
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.key_constraints WHERE name='uq_agent_guardrail_scope')
    ALTER TABLE agent_guardrails ADD CONSTRAINT uq_agent_guardrail_scope
        UNIQUE (scope_type, scope_id, agent_id, agent_type)
    """,
    """
    IF (SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='agent_guardrails' AND COLUMN_NAME='user_id') = 'NO'
    ALTER TABLE agent_guardrails ALTER COLUMN user_id INT NULL
    """,
    # ── Document scope columns ────────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_documents' AND COLUMN_NAME='scope')
    ALTER TABLE app_documents
        ADD scope    NVARCHAR(20) NOT NULL DEFAULT 'user',
            scope_id INT          NULL
    """,
    # ── Directory watch connector ─────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='document_watch_dirs')
    CREATE TABLE document_watch_dirs (
        id             INT IDENTITY(1,1) PRIMARY KEY,
        folder_path    NVARCHAR(1000) NOT NULL,
        label          NVARCHAR(255)  NOT NULL DEFAULT '',
        scope          NVARCHAR(20)   NOT NULL DEFAULT 'user',
        scope_id       INT            NULL,
        target_user_id INT            NULL,
        created_by     INT            NOT NULL DEFAULT 0,
        created_at     DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
        last_scanned   DATETIME2      NULL,
        enabled        BIT            NOT NULL DEFAULT 1,
        files_ingested INT            NOT NULL DEFAULT 0,
        CONSTRAINT uq_watch_dir UNIQUE (folder_path, target_user_id)
    )
    """,
    # ── SharePoint watch connector ────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='sharepoint_watch_configs')
    CREATE TABLE sharepoint_watch_configs (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        site_url         NVARCHAR(500)  NOT NULL,
        sp_folder_path   NVARCHAR(1000) NOT NULL DEFAULT '',
        label            NVARCHAR(255)  NOT NULL DEFAULT '',
        scope            NVARCHAR(20)   NOT NULL DEFAULT 'user',
        scope_id         INT            NULL,
        target_user_id   INT            NULL,
        created_by       INT            NOT NULL DEFAULT 0,
        created_at       DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
        last_scanned     DATETIME2      NULL,
        enabled          BIT            NOT NULL DEFAULT 1,
        files_ingested   INT            NOT NULL DEFAULT 0,
        poll_interval    INT            NOT NULL DEFAULT 60,
        cached_site_id   NVARCHAR(500)  NULL,
        cached_drive_id  NVARCHAR(500)  NULL
    )
    """,
    # ── Connector ingestion log ───────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='connector_logs')
    CREATE TABLE connector_logs (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        watch_type  NVARCHAR(20)   NOT NULL,
        watch_id    INT            NOT NULL,
        watch_label NVARCHAR(255)  NOT NULL DEFAULT '',
        filename    NVARCHAR(500)  NOT NULL DEFAULT '',
        status      NVARCHAR(20)   NOT NULL DEFAULT 'success',
        chunk_count INT            NOT NULL DEFAULT 0,
        error_msg   NVARCHAR(MAX)  NULL,
        created_at  DATETIME2      NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    # ── Dev proxy assignments (admin assigns which users a dev can proxy as) ──
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='dev_proxy_assignments')
    CREATE TABLE dev_proxy_assignments (
        id             INT IDENTITY(1,1) PRIMARY KEY,
        dev_user_id    INT       NOT NULL,
        target_user_id INT       NOT NULL,
        assigned_by    INT       NOT NULL DEFAULT 0,
        assigned_at    DATETIME2 NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_dev_proxy UNIQUE (dev_user_id, target_user_id)
    )
    """,
    # ── Retrieval trace log (observability) ───────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_retrieval_traces')
    CREATE TABLE app_retrieval_traces (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        session_id   NVARCHAR(100) NOT NULL DEFAULT '',
        user_id      INT           NOT NULL DEFAULT 0,
        query        NVARCHAR(MAX) NOT NULL,
        doc_ids      NVARCHAR(MAX) NULL,
        top_scores   NVARCHAR(MAX) NULL,
        method       NVARCHAR(50)  NOT NULL DEFAULT 'hybrid',
        result_count INT           NOT NULL DEFAULT 0,
        confidence   FLOAT         NULL,
        fallback     BIT           NOT NULL DEFAULT 0,
        latency_ms   INT           NULL,
        created_at   DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    # ── Search result cache ───────────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_search_cache')
    CREATE TABLE app_search_cache (
        cache_key    CHAR(32)      NOT NULL PRIMARY KEY,
        query        NVARCHAR(MAX) NOT NULL,
        user_id      INT           NOT NULL DEFAULT 0,
        results      NVARCHAR(MAX) NOT NULL,
        hit_count    INT           NOT NULL DEFAULT 1,
        created_at   DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        expires_at   DATETIME2     NOT NULL
    )
    """,
    # ── Persistent RAG session memory ────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_rag_sessions')
    CREATE TABLE app_rag_sessions (
        session_id  NVARCHAR(100) NOT NULL PRIMARY KEY,
        user_id     INT           NOT NULL DEFAULT 0,
        turns_json  NVARCHAR(MAX) NOT NULL DEFAULT '[]',
        updated_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    # ── RAG evaluation test queries ───────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_rag_eval_queries')
    CREATE TABLE app_rag_eval_queries (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        query            NVARCHAR(MAX) NOT NULL,
        expected_doc_ids NVARCHAR(MAX) NULL,
        category         NVARCHAR(100) NULL DEFAULT 'recall',
        created_at       DATETIME2     NOT NULL DEFAULT GETUTCDATE()
    )
    """,
    # ── RAG evaluation results ────────────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='app_rag_eval_results')
    CREATE TABLE app_rag_eval_results (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        eval_query_id   INT           NOT NULL,
        run_at          DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        recall_at_1     FLOAT         NULL,
        recall_at_3     FLOAT         NULL,
        recall_at_5     FLOAT         NULL,
        confidence      FLOAT         NULL,
        consistency     FLOAT         NULL,
        fallback        BIT           NOT NULL DEFAULT 0,
        latency_ms      INT           NULL,
        actual_doc_ids  NVARCHAR(MAX) NULL
    )
    """,
    # ── Key metrics on directory connector ───────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='document_watch_dirs' AND COLUMN_NAME='key_metrics')
    ALTER TABLE document_watch_dirs ADD key_metrics NVARCHAR(MAX) NULL
    """,
    # ── Key metrics on SharePoint connector ──────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='sharepoint_watch_configs' AND COLUMN_NAME='key_metrics')
    ALTER TABLE sharepoint_watch_configs ADD key_metrics NVARCHAR(MAX) NULL
    """,
    # ── Multi-user assignment list on directory connector ─────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='document_watch_dirs' AND COLUMN_NAME='user_ids_json')
    ALTER TABLE document_watch_dirs ADD user_ids_json NVARCHAR(MAX) NULL
    """,
    # ── Multi-user assignment list on SharePoint connector ────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='sharepoint_watch_configs' AND COLUMN_NAME='user_ids_json')
    ALTER TABLE sharepoint_watch_configs ADD user_ids_json NVARCHAR(MAX) NULL
    """,
    # ── Admin assigns resources (db connections, dirs, sharepoint) to devs ───
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='dev_resource_assignments')
    CREATE TABLE dev_resource_assignments (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        dev_user_id   INT          NOT NULL,
        resource_type NVARCHAR(50) NOT NULL,
        resource_id   INT          NOT NULL,
        assigned_by   INT          NOT NULL DEFAULT 0,
        assigned_at   DATETIME2    NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_dev_resource UNIQUE (dev_user_id, resource_type, resource_id)
    )
    """,
    # ── Ownership tracking on BI agents ──────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_agents' AND COLUMN_NAME='created_by')
    ALTER TABLE app_agents ADD created_by INT NULL DEFAULT 0
    """,
    # ── Ownership tracking on Hub agents ─────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_agents' AND COLUMN_NAME='created_by')
    ALTER TABLE hub_agents ADD created_by INT NULL DEFAULT 0
    """,
    # ── Semantic rules on BI agents ───────────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_agents' AND COLUMN_NAME='semantic_rules')
    ALTER TABLE app_agents ADD semantic_rules NVARCHAR(MAX) NULL
    """,
    # ── Portal project → local dept/project mapping ───────────────────────────
    # (OUR data — which external portal project maps to which local dept/project)
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='nexus_project_mapping')
    CREATE TABLE nexus_project_mapping (
        id                   INT IDENTITY(1,1) PRIMARY KEY,
        nexus_project_id     INT           NOT NULL,
        nexus_project_name   NVARCHAR(255) NOT NULL DEFAULT '',
        dept_id              INT           NULL,
        project_id           INT           NULL,
        created_by           INT           NOT NULL DEFAULT 0,
        created_at           DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_nexus_project_mapping UNIQUE (nexus_project_id)
    )
    """,
    # ── Portal user assignment tracking ───────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='nexus_user_sync')
    CREATE TABLE nexus_user_sync (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        nexus_user_id   INT           NULL,
        local_user_id   INT           NULL,
        user_email      NVARCHAR(255) NOT NULL DEFAULT '',
        last_synced_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        sync_status     NVARCHAR(50)  NOT NULL DEFAULT 'pending'
    )
    """,
    # ── BI agent conversation persistence ────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='bi_conversations')
    CREATE TABLE bi_conversations (
        id         NVARCHAR(36)  NOT NULL PRIMARY KEY,
        agent_name NVARCHAR(255) NOT NULL,
        user_id    INT           NOT NULL,
        title      NVARCHAR(500) NOT NULL DEFAULT 'New Conversation',
        created_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        updated_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        INDEX ix_bi_conv_user  NONCLUSTERED (user_id),
        INDEX ix_bi_conv_agent NONCLUSTERED (agent_name),
        INDEX ix_bi_conv_upd   NONCLUSTERED (updated_at DESC)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='bi_messages')
    CREATE TABLE bi_messages (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        conversation_id NVARCHAR(36)  NOT NULL,
        role            NVARCHAR(20)  NOT NULL,
        content         NVARCHAR(MAX) NOT NULL,
        sql_query       NVARCHAR(MAX) NULL,
        tokens_used     INT           NULL,
        created_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        INDEX ix_bi_msg_conv NONCLUSTERED (conversation_id)
    )
    """,
    # ── Generated dashboard/report/infographic persistence ───────────────────
    # One row per (conversation_id, artifact_type) — a fresh "Generate" call
    # overwrites the previous row for that type via MERGE, so reopening a
    # conversation can restore the last-generated artifact without re-calling
    # the LLM. See database/bi_conversations_db.py save_artifact()/get_artifacts().
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='bi_generated_artifacts')
    CREATE TABLE bi_generated_artifacts (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        conversation_id NVARCHAR(36)  NOT NULL,
        artifact_type   NVARCHAR(20)  NOT NULL,   -- 'dashboard' | 'infographic' | 'report'
        cache_key       NVARCHAR(500) NOT NULL,   -- fingerprint computed client-side (see modals.html)
        payload_json    NVARCHAR(MAX) NOT NULL,   -- the generation API's own response JSON, verbatim
        created_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        updated_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_bi_artifact UNIQUE (conversation_id, artifact_type),
        INDEX ix_bi_artifact_conv NONCLUSTERED (conversation_id)
    )
    """,
    # ── Drop unused bulk-storage columns from bi_messages ────────────────────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='bi_messages' AND COLUMN_NAME='columns_json')
        ALTER TABLE bi_messages DROP COLUMN columns_json
    """,
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
               WHERE TABLE_NAME='bi_messages' AND COLUMN_NAME='data_json')
        ALTER TABLE bi_messages DROP COLUMN data_json
    """,
    # ── Separate input / output token columns on hub_messages ────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_messages' AND COLUMN_NAME='input_tokens')
    BEGIN
        ALTER TABLE hub_messages ADD input_tokens  INT NOT NULL DEFAULT 0
        ALTER TABLE hub_messages ADD output_tokens INT NOT NULL DEFAULT 0
    END
    """,
    # ── Separate input / output token columns on token_usage ─────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='token_usage' AND COLUMN_NAME='input_tokens')
    BEGIN
        ALTER TABLE token_usage ADD input_tokens  INT NOT NULL DEFAULT 0
        ALTER TABLE token_usage ADD output_tokens INT NOT NULL DEFAULT 0
    END
    """,
    # ── Model column on token_usage — records the exact model each call ran
    # on, so cost can be priced per-row instead of assuming one model (e.g.
    # gpt-4o) for every call, or joining to a hub agent's *current* model
    # setting (wrong for historical rows if the agent's model was changed).
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='token_usage' AND COLUMN_NAME='model')
    ALTER TABLE token_usage ADD model NVARCHAR(100) NULL
    """,
    # ── Agent-ID column on token_usage — a stable reference (hub_agents.id)
    # for hub_chat/hub_workflow rows, so analytics can join on ID instead of
    # the mutable agent_name string. A renamed hub agent would otherwise lose
    # its historical usage from any name-keyed join (e.g. department/project
    # filtered views on the Money page).
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='token_usage' AND COLUMN_NAME='agent_id')
    ALTER TABLE token_usage ADD agent_id NVARCHAR(36) NULL
    """,
    # ── Many-to-many: resource ↔ departments ─────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_departments')
    CREATE TABLE resource_departments (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        resource_type NVARCHAR(20)  NOT NULL,
        resource_id   NVARCHAR(100) NOT NULL,
        dept_id       INT           NOT NULL,
        assigned_at   DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_res_dept UNIQUE (resource_type, resource_id, dept_id)
    )
    """,
    # ── Many-to-many: resource ↔ projects ────────────────────────────────────
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_projects')
    CREATE TABLE resource_projects (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        resource_type NVARCHAR(20)  NOT NULL,
        resource_id   NVARCHAR(100) NOT NULL,
        project_id    INT           NOT NULL,
        assigned_at   DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_res_proj UNIQUE (resource_type, resource_id, project_id)
    )
    """,
    # ── Migrate existing app_agents single assignments → junction tables ──────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_departments')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_agents' AND COLUMN_NAME='department_id')
        BEGIN
            INSERT INTO resource_departments (resource_type, resource_id, dept_id)
            SELECT 'app_agent', a.name, a.department_id
            FROM   app_agents a
            WHERE  a.department_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_departments rd
                WHERE  rd.resource_type='app_agent'
                  AND  rd.resource_id=a.name
                  AND  rd.dept_id=a.department_id
              )
        END
    END
    """,
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_projects')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_agents' AND COLUMN_NAME='project_id')
        BEGIN
            INSERT INTO resource_projects (resource_type, resource_id, project_id)
            SELECT 'app_agent', a.name, a.project_id
            FROM   app_agents a
            WHERE  a.project_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_projects rp
                WHERE  rp.resource_type='app_agent'
                  AND  rp.resource_id=a.name
                  AND  rp.project_id=a.project_id
              )
        END
    END
    """,
    # ── Migrate existing hub_agents single assignments → junction tables ───────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_departments')
       AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_agents')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_agents' AND COLUMN_NAME='department_id')
        BEGIN
            INSERT INTO resource_departments (resource_type, resource_id, dept_id)
            SELECT 'hub_agent', CAST(a.id AS NVARCHAR(100)), a.department_id
            FROM   hub_agents a
            WHERE  a.department_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_departments rd
                WHERE  rd.resource_type='hub_agent'
                  AND  rd.resource_id=CAST(a.id AS NVARCHAR(100))
                  AND  rd.dept_id=a.department_id
              )
        END
    END
    """,
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_projects')
       AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_agents')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_agents' AND COLUMN_NAME='project_id')
        BEGIN
            INSERT INTO resource_projects (resource_type, resource_id, project_id)
            SELECT 'hub_agent', CAST(a.id AS NVARCHAR(100)), a.project_id
            FROM   hub_agents a
            WHERE  a.project_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_projects rp
                WHERE  rp.resource_type='hub_agent'
                  AND  rp.resource_id=CAST(a.id AS NVARCHAR(100))
                  AND  rp.project_id=a.project_id
              )
        END
    END
    """,
    # ── Migrate hub_workflows ─────────────────────────────────────────────────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_departments')
       AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_workflows')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_workflows' AND COLUMN_NAME='department_id')
        BEGIN
            INSERT INTO resource_departments (resource_type, resource_id, dept_id)
            SELECT 'hub_workflow', CAST(w.id AS NVARCHAR(100)), w.department_id
            FROM   hub_workflows w
            WHERE  w.department_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_departments rd
                WHERE  rd.resource_type='hub_workflow'
                  AND  rd.resource_id=CAST(w.id AS NVARCHAR(100))
                  AND  rd.dept_id=w.department_id
              )
        END
    END
    """,
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_projects')
       AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_workflows')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_workflows' AND COLUMN_NAME='project_id')
        BEGIN
            INSERT INTO resource_projects (resource_type, resource_id, project_id)
            SELECT 'hub_workflow', CAST(w.id AS NVARCHAR(100)), w.project_id
            FROM   hub_workflows w
            WHERE  w.project_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_projects rp
                WHERE  rp.resource_type='hub_workflow'
                  AND  rp.resource_id=CAST(w.id AS NVARCHAR(100))
                  AND  rp.project_id=w.project_id
              )
        END
    END
    """,
    # ── Migrate app_jobs ──────────────────────────────────────────────────────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_departments')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_jobs' AND COLUMN_NAME='department_id')
        BEGIN
            INSERT INTO resource_departments (resource_type, resource_id, dept_id)
            SELECT 'app_job', CAST(j.id AS NVARCHAR(100)), j.department_id
            FROM   app_jobs j
            WHERE  j.department_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_departments rd
                WHERE  rd.resource_type='app_job'
                  AND  rd.resource_id=CAST(j.id AS NVARCHAR(100))
                  AND  rd.dept_id=j.department_id
              )
        END
    END
    """,
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_projects')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='app_jobs' AND COLUMN_NAME='project_id')
        BEGIN
            INSERT INTO resource_projects (resource_type, resource_id, project_id)
            SELECT 'app_job', CAST(j.id AS NVARCHAR(100)), j.project_id
            FROM   app_jobs j
            WHERE  j.project_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_projects rp
                WHERE  rp.resource_type='app_job'
                  AND  rp.resource_id=CAST(j.id AS NVARCHAR(100))
                  AND  rp.project_id=j.project_id
              )
        END
    END
    """,
    # ── Migrate hub_jobs ──────────────────────────────────────────────────────
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_departments')
       AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_jobs')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_jobs' AND COLUMN_NAME='department_id')
        BEGIN
            INSERT INTO resource_departments (resource_type, resource_id, dept_id)
            SELECT 'hub_job', CAST(j.id AS NVARCHAR(100)), j.department_id
            FROM   hub_jobs j
            WHERE  j.department_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_departments rd
                WHERE  rd.resource_type='hub_job'
                  AND  rd.resource_id=CAST(j.id AS NVARCHAR(100))
                  AND  rd.dept_id=j.department_id
              )
        END
    END
    """,
    """
    IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='resource_projects')
       AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='hub_jobs')
    BEGIN
        IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='hub_jobs' AND COLUMN_NAME='project_id')
        BEGIN
            INSERT INTO resource_projects (resource_type, resource_id, project_id)
            SELECT 'hub_job', CAST(j.id AS NVARCHAR(100)), j.project_id
            FROM   hub_jobs j
            WHERE  j.project_id IS NOT NULL
              AND  NOT EXISTS (
                SELECT 1 FROM resource_projects rp
                WHERE  rp.resource_type='hub_job'
                  AND  rp.resource_id=CAST(j.id AS NVARCHAR(100))
                  AND  rp.project_id=j.project_id
              )
        END
    END
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='document_profiles')
    CREATE TABLE document_profiles (
        id                INT IDENTITY(1,1) PRIMARY KEY,
        doc_id            NVARCHAR(200)  NOT NULL,
        source_file       NVARCHAR(500)  NOT NULL DEFAULT '',
        source_type       NVARCHAR(50)   NOT NULL DEFAULT 'knowledge',
        connector_key     NVARCHAR(200)  NULL,
        document_type     NVARCHAR(100)  NOT NULL DEFAULT 'general',
        summary           NVARCHAR(MAX)  NULL,
        key_metrics       NVARCHAR(MAX)  NULL,
        summary_embedding NVARCHAR(MAX)  NULL,
        processed_at      DATETIME2      NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT uq_document_profiles_doc_id UNIQUE (doc_id),
        INDEX ix_document_profiles_connector_key NONCLUSTERED (connector_key),
        INDEX ix_document_profiles_source_type   NONCLUSTERED (source_type)
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME='document_profiles' AND COLUMN_NAME='summary_embedding')
    ALTER TABLE document_profiles ADD summary_embedding NVARCHAR(MAX) NULL
    """,
]


def ensure_tables() -> None:
    """Create all app-internal tables if they don't already exist."""
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            for stmt in _TABLE_STMTS:
                cursor.execute(stmt)
            # Add token_quota column to users table if it doesn't exist yet
            cursor.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME='users' AND COLUMN_NAME='token_quota'
                )
                ALTER TABLE users ADD token_quota INT NULL
            """)
            conn.commit()
        logger.info("app_db: tables verified/created")
    except Exception as exc:
        logger.error("app_db: failed to ensure tables: %s", exc)


# Cross-database view names used throughout the app.
# PORTAL_DB_NAME is operator-configured (config.py / PORTAL_DB_NAME env var) —
# nothing here hardcodes a specific external system's name. If unset, portal
# sync is simply disabled.
_PORTAL_VIEW_STMTS_TEMPLATE = [
    # Users view
    """
    CREATE OR ALTER VIEW nexus_view_users AS
    SELECT
        Id           AS nexus_user_id,
        UserEmail    AS user_email,
        FirstName    AS first_name,
        LastName     AS last_name,
        Status       AS status,
        IsActive     AS is_active,
        EmployeeId   AS employee_id
    FROM [{db}].[dbo].[Users]
    """,
    # Projects view
    """
    CREATE OR ALTER VIEW nexus_view_projects AS
    SELECT
        Id       AS nexus_project_id,
        Name     AS project_name,
        IsActive AS is_active
    FROM [{db}].[dbo].[Projects]
    """,
    # Assignments view (joins all three for easy lookup)
    """
    CREATE OR ALTER VIEW nexus_view_user_projects AS
    SELECT
        pa.Id        AS assignment_id,
        pa.UserId    AS nexus_user_id,
        pa.ProjectId AS nexus_project_id,
        pa.ManagerId AS manager_id,
        u.UserEmail  AS user_email,
        u.FirstName  AS first_name,
        u.LastName   AS last_name,
        u.IsActive   AS user_is_active,
        p.Name       AS project_name,
        p.IsActive   AS project_is_active
    FROM [{db}].[dbo].[ProjectAssignmentToUsers] pa
    JOIN [{db}].[dbo].[Users]    u ON u.Id = pa.UserId
    JOIN [{db}].[dbo].[Projects] p ON p.Id = pa.ProjectId
    """,
]


def ensure_portal_views() -> bool:
    """
    Create (or replace) cross-database views in this DB that point directly
    at the configured external portal database (``config.PORTAL_DB_NAME``)
    on the same SQL Server instance. Views are always live — no sync needed.

    No-ops (returns False) if PORTAL_DB_NAME isn't configured. Returns True
    on success, False if the portal database is unreachable (non-fatal).
    """
    if not config.PORTAL_DB_NAME:
        logger.info("app_db: portal sync disabled (set PORTAL_DB_NAME to enable)")
        return False
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            for stmt in _PORTAL_VIEW_STMTS_TEMPLATE:
                cursor.execute(stmt.format(db=config.PORTAL_DB_NAME))
            conn.commit()
        logger.info("app_db: portal cross-database views created/updated")
        return True
    except Exception as exc:
        logger.warning(
            "app_db: could not create portal views "
            "(portal DB may be on a different instance or unreachable): %s", exc
        )
        return False


def log_connector_event(
    watch_type: str,
    watch_id: int,
    watch_label: str,
    filename: str,
    status: str,           # 'success' | 'error' | 'skipped'
    chunk_count: int = 0,
    error_msg: str = "",
) -> None:
    """Insert one row into connector_logs. Fire-and-forget; never raises."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO connector_logs
                    (watch_type, watch_id, watch_label, filename, status, chunk_count, error_msg)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, watch_type, watch_id, watch_label or "", filename or "",
                status, chunk_count, error_msg or None)
            conn.commit()
    except Exception as exc:
        logger.warning("log_connector_event: %s", exc)


# ── Dev resource assignment helpers ──────────────────────────────────────────

def get_dev_assigned_resource_ids(dev_user_id: int, resource_type: str) -> list:
    """Return list of resource IDs (INT) assigned to a dev user of the given type.

    resource_type: 'db_connection' | 'watch_dir' | 'sharepoint_watch'
    """
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT resource_id FROM dev_resource_assignments "
                "WHERE dev_user_id=? AND resource_type=?",
                dev_user_id, resource_type,
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("get_dev_assigned_resource_ids: %s", exc)
        return []


def assign_resource_to_dev(dev_user_id: int, resource_type: str,
                            resource_id: int, assigned_by: int) -> tuple:
    """Grant a dev user access to one resource (db_connection/watch_dir/sharepoint_watch); idempotent."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                IF NOT EXISTS (
                    SELECT 1 FROM dev_resource_assignments
                    WHERE dev_user_id=? AND resource_type=? AND resource_id=?
                )
                INSERT INTO dev_resource_assignments
                    (dev_user_id, resource_type, resource_id, assigned_by)
                VALUES (?, ?, ?, ?)
            """, dev_user_id, resource_type, resource_id,
                dev_user_id, resource_type, resource_id, assigned_by)
            conn.commit()
        return True, "Resource assigned"
    except Exception as exc:
        logger.exception("assign_resource_to_dev failed")
        return False, str(exc)


def unassign_resource_from_dev(dev_user_id: int, resource_type: str,
                                resource_id: int) -> tuple:
    """Revoke a dev user's access to one previously-assigned resource."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM dev_resource_assignments "
                "WHERE dev_user_id=? AND resource_type=? AND resource_id=?",
                dev_user_id, resource_type, resource_id,
            )
            conn.commit()
        return True, "Resource unassigned"
    except Exception as exc:
        logger.exception("unassign_resource_from_dev failed")
        return False, str(exc)


def get_all_dev_resource_assignments(dev_user_id: int = None) -> list:
    """Return all dev resource assignments, optionally filtered by dev_user_id."""
    try:
        with get_app_db() as conn:
            cur = conn.cursor()
            if dev_user_id:
                cur.execute("""
                    SELECT dra.id, dra.dev_user_id, u.username AS dev_username,
                           dra.resource_type, dra.resource_id, dra.assigned_by,
                           ab.username AS assigned_by_name, dra.assigned_at
                    FROM   dev_resource_assignments dra
                    JOIN   users u  ON u.id  = dra.dev_user_id
                    JOIN   users ab ON ab.id = dra.assigned_by
                    WHERE  dra.dev_user_id = ?
                    ORDER BY dra.resource_type, dra.assigned_at DESC
                """, dev_user_id)
            else:
                cur.execute("""
                    SELECT dra.id, dra.dev_user_id, u.username AS dev_username,
                           dra.resource_type, dra.resource_id, dra.assigned_by,
                           ab.username AS assigned_by_name, dra.assigned_at
                    FROM   dev_resource_assignments dra
                    JOIN   users u  ON u.id  = dra.dev_user_id
                    JOIN   users ab ON ab.id = dra.assigned_by
                    ORDER BY dra.dev_user_id, dra.resource_type, dra.assigned_at DESC
                """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            for r in rows:
                if r.get("assigned_at"):
                    r["assigned_at"] = str(r["assigned_at"])
            return rows
    except Exception as exc:
        logger.warning("get_all_dev_resource_assignments: %s", exc)
        return []


# ── one-time JSON migration ───────────────────────────────────────────────────

def migrate_from_json() -> None:
    """
    If any app table is empty and the legacy JSON file exists,
    import the JSON data into the table.  Idempotent — skips tables
    that already have rows.
    """
    _migrate_connections()
    _migrate_agents()
    _migrate_jobs()
    _migrate_executions()
    _migrate_knowledge()
    _migrate_learning()
    _migrate_clients()


def _migrate_connections() -> None:
    """One-time seed of app_database_connections from the legacy database_connections.json, if empty."""
    json_path = Path("Data/Database/database_connections.json")
    if not json_path.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_database_connections")
            if cursor.fetchone()[0] > 0:
                return
            data = json.loads(json_path.read_text())
            for c in data:
                cursor.execute("""
                    INSERT INTO app_database_connections
                        (name, type, server, port, username, password, db_name, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, c.get("name",""), c.get("type",""), c.get("server",""),
                    str(c.get("port","")), c.get("username",""), c.get("password",""),
                    c.get("database",""), c.get("status","disconnected"))
            conn.commit()
        logger.info("app_db: migrated %d database connections from JSON", len(data))
    except Exception as exc:
        logger.warning("app_db: connection migration failed: %s", exc)


def _migrate_agents() -> None:
    """One-time seed of app_agents from the legacy agents.json, if empty."""
    json_path = Path("Data/Agents/agents.json")
    if not json_path.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_agents")
            if cursor.fetchone()[0] > 0:
                return
            data = json.loads(json_path.read_text())
            for a in data:
                cursor.execute("""
                    INSERT INTO app_agents
                        (name, description, database_connection,
                         selected_tables, selected_columns, schema_context, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, a.get("name",""), a.get("description",""),
                    a.get("database_connection",""),
                    json.dumps(a.get("selected_tables", [])),
                    json.dumps(a.get("selected_columns", {})),
                    json.dumps(a.get("schema_context")) if a.get("schema_context") else None,
                    a.get("created_at", datetime.utcnow().isoformat()))
            conn.commit()
        logger.info("app_db: migrated %d agents from JSON", len(data))
    except Exception as exc:
        logger.warning("app_db: agent migration failed: %s", exc)


def _migrate_jobs() -> None:
    """One-time seed of app_jobs from the legacy jobs.json, if empty."""
    json_path = Path("Data/Jobs/jobs.json")
    if not json_path.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_jobs")
            if cursor.fetchone()[0] > 0:
                return
            data = json.loads(json_path.read_text())
            for j in data:
                cursor.execute("""
                    INSERT INTO app_jobs
                        (id, name, description, agent_name, nlq_prompt,
                         tools, schedule, delivery, enabled,
                         created_by, created_by_username, created_by_role,
                         created_at, updated_at, last_run, next_run,
                         run_count, last_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    j.get("id", ""), j.get("name",""), j.get("description",""),
                    j.get("agent_name",""), j.get("nlq_prompt",""),
                    json.dumps(j.get("tools",[])),
                    json.dumps(j.get("schedule",{})),
                    json.dumps(j.get("delivery",{})),
                    1 if j.get("enabled", True) else 0,
                    j.get("created_by", 0), j.get("created_by_username","system"),
                    j.get("created_by_role","user"),
                    j.get("created_at", datetime.utcnow().isoformat()),
                    j.get("updated_at", datetime.utcnow().isoformat()),
                    j.get("last_run"), j.get("next_run"),
                    j.get("run_count", 0), j.get("last_status"))
            conn.commit()
        logger.info("app_db: migrated %d jobs from JSON", len(data))
    except Exception as exc:
        logger.warning("app_db: job migration failed: %s", exc)


def _migrate_executions() -> None:
    """One-time seed of app_job_executions from legacy per-job JSON log files (last 100 entries each), if empty."""
    logs_dir = Path("Data/Jobs/logs")
    if not logs_dir.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_job_executions")
            if cursor.fetchone()[0] > 0:
                return
            count = 0
            for log_file in logs_dir.glob("*.json"):
                try:
                    entries = json.loads(log_file.read_text())
                    for e in entries[-100:]:
                        cursor.execute("""
                            INSERT INTO app_job_executions
                                (exec_id, job_id, job_name, triggered_by, status,
                                 started_at, finished_at, duration_ms, error,
                                 tools_completed, row_count, artifacts,
                                 query_result, log_lines)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            e.get("exec_id",""), e.get("job_id",""),
                            e.get("job_name",""), e.get("triggered_by","scheduler"),
                            e.get("status","running"),
                            e.get("started_at"), e.get("finished_at"),
                            e.get("duration_ms"), e.get("error"),
                            json.dumps(e.get("tools_completed",[])),
                            e.get("row_count", 0),
                            json.dumps(e.get("artifacts",[])),
                            json.dumps(e.get("query_result")) if e.get("query_result") else None,
                            json.dumps(e.get("log_lines",[])))
                        count += 1
                except Exception:
                    pass
            conn.commit()
        logger.info("app_db: migrated %d job execution logs from JSON", count)
    except Exception as exc:
        logger.warning("app_db: execution log migration failed: %s", exc)


def _migrate_knowledge() -> None:
    """One-time seed of app_knowledge_store from the legacy knowledge.json, if empty."""
    json_path = Path("Data/agent_store/knowledge.json")
    if not json_path.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_knowledge_store")
            if cursor.fetchone()[0] > 0:
                return
            data = json.loads(json_path.read_text())
            for key, entry in data.items():
                cursor.execute("""
                    INSERT INTO app_knowledge_store (key_name, value, updated_at)
                    VALUES (?, ?, ?)
                """, key, entry.get("value",""), entry.get("updated", datetime.utcnow().isoformat()))
            conn.commit()
        logger.info("app_db: migrated %d knowledge entries from JSON", len(data))
    except Exception as exc:
        logger.warning("app_db: knowledge migration failed: %s", exc)


def _migrate_learning() -> None:
    """One-time seed of app_nlq_learning from the legacy learning_store.json, if empty."""
    json_path = Path("Data/Agents/learning_store.json")
    if not json_path.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_nlq_learning")
            if cursor.fetchone()[0] > 0:
                return
            data = json.loads(json_path.read_text())
            for agent_name, agent_data in data.items():
                cursor.execute("""
                    INSERT INTO app_nlq_learning (agent_name, data)
                    VALUES (?, ?)
                """, agent_name, json.dumps(agent_data))
            conn.commit()
        logger.info("app_db: migrated learning store for %d agents from JSON", len(data))
    except Exception as exc:
        logger.warning("app_db: learning store migration failed: %s", exc)


def _migrate_clients() -> None:
    """One-time seed of app_clients from the legacy clients.json, if empty."""
    json_path = Path("clients.json")
    if not json_path.exists():
        return
    try:
        with get_app_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM app_clients")
            if cursor.fetchone()[0] > 0:
                return
            data = json.loads(json_path.read_text(encoding="utf-8"))
            for c in data:
                # Extract known fields; remainder goes into extra_data
                known = {"id", "name", "email_db_config", "all_our_services", "ppt_history"}
                extra = {k: v for k, v in c.items() if k not in known}
                cursor.execute("""
                    INSERT INTO app_clients
                        (id, name, email_db_config, all_our_services, ppt_history, extra_data)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    c.get("id", ""),
                    c.get("name", ""),
                    json.dumps(c.get("email_db_config") or {}),
                    json.dumps(c.get("all_our_services") or []),
                    json.dumps(c.get("ppt_history") or []),
                    json.dumps(extra))
            conn.commit()
        logger.info("app_db: migrated %d clients from clients.json", len(data))
    except Exception as exc:
        logger.warning("app_db: clients migration failed: %s", exc)
