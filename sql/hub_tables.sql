-- ============================================================
-- hub_tables.sql
-- Run in SSMS against the nexus database.
-- Migrates all Agents Hub tables from SQLite to SQL Server.
--
-- IMPORTANT: Drops the old tracking-only hub_conversations and
-- hub_workflow_runs tables and replaces them with full tables.
-- Run hub_approvals.sql first if you haven't already.
-- ============================================================

-- Drop old lightweight tracking tables (replaced by full versions below)
IF OBJECT_ID('hub_conversations',  'U') IS NOT NULL DROP TABLE hub_conversations;
IF OBJECT_ID('hub_workflow_runs',  'U') IS NOT NULL DROP TABLE hub_workflow_runs;
GO

-- ── Agents ───────────────────────────────────────────────────────────────────
IF OBJECT_ID('hub_agents', 'U') IS NULL
BEGIN
    CREATE TABLE hub_agents (
        id            NVARCHAR(36)   NOT NULL PRIMARY KEY,
        name          NVARCHAR(200)  NOT NULL DEFAULT 'New Agent',
        description   NVARCHAR(MAX)  NULL,
        objective     NVARCHAR(MAX)  NULL,
        system_prompt NVARCHAR(MAX)  NULL,
        model         NVARCHAR(100)  NOT NULL DEFAULT 'gpt-4o',
        provider      NVARCHAR(20)   NOT NULL DEFAULT 'openai',
        temperature   FLOAT          NOT NULL DEFAULT 0.7,
        tools_json    NVARCHAR(MAX)  NOT NULL DEFAULT '[]',
        avatar_color  NVARCHAR(20)   NULL     DEFAULT '#6366f1',
        env_vars_json NVARCHAR(MAX)  NULL     DEFAULT '{}',
        status        NVARCHAR(20)   NOT NULL DEFAULT 'active',
        total_runs    INT            NOT NULL DEFAULT 0,
        total_tokens  INT            NOT NULL DEFAULT 0,
        department_id INT            NULL,
        project_id    INT            NULL,
        created_by    INT            NULL,
        created_at    DATETIME2               DEFAULT GETDATE(),
        updated_at    DATETIME2               DEFAULT GETDATE()
    );
    CREATE INDEX ix_hub_agents_status ON hub_agents(status);
    CREATE INDEX ix_hub_agents_dept   ON hub_agents(department_id);
    CREATE INDEX ix_hub_agents_proj   ON hub_agents(project_id);
    PRINT 'hub_agents created.';
END
GO

-- ── Conversations (full data, replaces old tracking-only table) ───────────────
CREATE TABLE hub_conversations (
    id         NVARCHAR(36)   NOT NULL PRIMARY KEY,
    agent_id   NVARCHAR(36)   NOT NULL,
    user_id    INT            NOT NULL,
    title      NVARCHAR(500)  NOT NULL DEFAULT 'New Conversation',
    created_at DATETIME2               DEFAULT GETDATE(),
    updated_at DATETIME2               DEFAULT GETDATE()
);
CREATE INDEX ix_hub_conversations_agent ON hub_conversations(agent_id);
CREATE INDEX ix_hub_conversations_user  ON hub_conversations(user_id);
PRINT 'hub_conversations created.';
GO

-- ── Messages ──────────────────────────────────────────────────────────────────
IF OBJECT_ID('hub_messages', 'U') IS NULL
BEGIN
    CREATE TABLE hub_messages (
        id              NVARCHAR(36)   NOT NULL PRIMARY KEY,
        conversation_id NVARCHAR(36)   NOT NULL,
        role            NVARCHAR(20)   NOT NULL,
        content         NVARCHAR(MAX)  NULL,
        tool_calls_json NVARCHAR(MAX)  NULL DEFAULT '[]',
        tokens_used     INT            NOT NULL DEFAULT 0,
        input_tokens    INT            NOT NULL DEFAULT 0,
        output_tokens   INT            NOT NULL DEFAULT 0,
        created_at      DATETIME2               DEFAULT GETDATE()
    );
    CREATE INDEX ix_hub_messages_convo ON hub_messages(conversation_id);
    PRINT 'hub_messages created.';
END
GO

-- ── Tools ─────────────────────────────────────────────────────────────────────
IF OBJECT_ID('hub_tools', 'U') IS NULL
BEGIN
    CREATE TABLE hub_tools (
        id            NVARCHAR(100)  NOT NULL PRIMARY KEY,
        name          NVARCHAR(100)  NOT NULL UNIQUE,
        display_name  NVARCHAR(200)  NOT NULL,
        description   NVARCHAR(MAX)  NULL,
        category      NVARCHAR(100)  NULL,
        schema_json   NVARCHAR(MAX)  NULL DEFAULT '{}',
        enabled       BIT            NOT NULL DEFAULT 1,
        total_calls   INT            NOT NULL DEFAULT 0,
        success_calls INT            NOT NULL DEFAULT 0,
        created_at    DATETIME2               DEFAULT GETDATE()
    );
    PRINT 'hub_tools created.';
END
GO

-- ── Workflows ─────────────────────────────────────────────────────────────────
IF OBJECT_ID('hub_workflows', 'U') IS NULL
BEGIN
    CREATE TABLE hub_workflows (
        id             NVARCHAR(36)   NOT NULL PRIMARY KEY,
        name           NVARCHAR(200)  NOT NULL DEFAULT 'New Workflow',
        description    NVARCHAR(MAX)  NULL,
        nodes_json     NVARCHAR(MAX)  NOT NULL DEFAULT '[]',
        edges_json     NVARCHAR(MAX)  NOT NULL DEFAULT '[]',
        execution_mode NVARCHAR(20)   NOT NULL DEFAULT 'sequential',
        status         NVARCHAR(20)   NOT NULL DEFAULT 'active',
        total_runs     INT            NOT NULL DEFAULT 0,
        department_id  INT            NULL,
        project_id     INT            NULL,
        created_at     DATETIME2               DEFAULT GETDATE(),
        updated_at     DATETIME2               DEFAULT GETDATE()
    );
    CREATE INDEX ix_hub_workflows_dept ON hub_workflows(department_id);
    CREATE INDEX ix_hub_workflows_proj ON hub_workflows(project_id);
    PRINT 'hub_workflows created.';
END
GO

-- ── Workflow Runs (full data, replaces old tracking-only table) ───────────────
CREATE TABLE hub_workflow_runs (
    id                 NVARCHAR(36)   NOT NULL PRIMARY KEY,
    workflow_id        NVARCHAR(36)   NOT NULL,
    user_id            INT            NOT NULL,
    status             NVARCHAR(20)   NOT NULL DEFAULT 'running',
    input_data         NVARCHAR(MAX)  NULL,
    output_data        NVARCHAR(MAX)  NULL,
    execution_log_json NVARCHAR(MAX)  NULL,
    started_at         DATETIME2               DEFAULT GETDATE(),
    completed_at       DATETIME2      NULL
);
CREATE INDEX ix_hub_wf_runs_workflow ON hub_workflow_runs(workflow_id);
CREATE INDEX ix_hub_wf_runs_user     ON hub_workflow_runs(user_id);
PRINT 'hub_workflow_runs created.';
GO

-- ── Knowledge Bases ───────────────────────────────────────────────────────────
IF OBJECT_ID('hub_knowledge_bases', 'U') IS NULL
BEGIN
    CREATE TABLE hub_knowledge_bases (
        id          NVARCHAR(36)   NOT NULL PRIMARY KEY,
        name        NVARCHAR(200)  NOT NULL DEFAULT 'Document',
        description NVARCHAR(MAX)  NULL,
        file_type   NVARCHAR(20)   NOT NULL DEFAULT 'txt',
        content     NVARCHAR(MAX)  NULL,
        file_size   INT            NOT NULL DEFAULT 0,
        chunk_count INT            NOT NULL DEFAULT 0,
        created_at  DATETIME2               DEFAULT GETDATE()
    );
    PRINT 'hub_knowledge_bases created.';
END
GO

-- ── Jobs / Scheduler ──────────────────────────────────────────────────────────
IF OBJECT_ID('hub_jobs', 'U') IS NULL
BEGIN
    CREATE TABLE hub_jobs (
        id          NVARCHAR(36)   NOT NULL PRIMARY KEY,
        name        NVARCHAR(200)  NOT NULL DEFAULT 'New Job',
        description NVARCHAR(MAX)  NULL,
        job_type    NVARCHAR(50)   NOT NULL DEFAULT 'agent',
        target_id   NVARCHAR(36)   NULL,
        schedule    NVARCHAR(100)  NOT NULL DEFAULT '0 9 * * *',
        status      NVARCHAR(20)   NOT NULL DEFAULT 'active',
        created_at  DATETIME2               DEFAULT GETDATE()
    );
    PRINT 'hub_jobs created.';
END
GO

PRINT 'All hub tables created successfully.';
GO
