-- =============================================================
-- AGENTS HUB INTEGRATION  |  SQL Server (SSMS)
-- Run this ONCE against your application database AFTER
-- auth_setup.sql has already been executed.
-- =============================================================

USE [nexus];
GO

-- -------------------------------------------------------------
-- 1. HUB AGENT ASSIGNMENTS
--    Tracks which agents (from agents/aihub.db SQLite) are
--    assigned to which SQL Server users.
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'hub_agent_assignments')
BEGIN
    CREATE TABLE hub_agent_assignments (
        id          INT           IDENTITY(1,1) PRIMARY KEY,
        user_id     INT           NOT NULL,
        agent_id    NVARCHAR(36)  NOT NULL,      -- UUID from agents SQLite
        agent_name  NVARCHAR(255) NOT NULL,
        assigned_by INT           NOT NULL,
        assigned_at DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_haa_user     FOREIGN KEY (user_id)     REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_haa_assigner FOREIGN KEY (assigned_by) REFERENCES users(id),
        CONSTRAINT uq_haa_user_agent UNIQUE (user_id, agent_id)
    );

    CREATE INDEX IX_haa_user_id   ON hub_agent_assignments(user_id);
    CREATE INDEX IX_haa_agent_id  ON hub_agent_assignments(agent_id);
    PRINT '✅ hub_agent_assignments table created.';
END
ELSE
    PRINT '⚠️  hub_agent_assignments already exists — skipped.';
GO

-- -------------------------------------------------------------
-- 2. HUB WORKFLOW ASSIGNMENTS
--    Tracks which workflows are assigned to which users.
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'hub_workflow_assignments')
BEGIN
    CREATE TABLE hub_workflow_assignments (
        id            INT           IDENTITY(1,1) PRIMARY KEY,
        user_id       INT           NOT NULL,
        workflow_id   NVARCHAR(36)  NOT NULL,     -- UUID from agents SQLite
        workflow_name NVARCHAR(255) NOT NULL,
        assigned_by   INT           NOT NULL,
        assigned_at   DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_hwa_user     FOREIGN KEY (user_id)     REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_hwa_assigner FOREIGN KEY (assigned_by) REFERENCES users(id),
        CONSTRAINT uq_hwa_user_wf  UNIQUE (user_id, workflow_id)
    );

    CREATE INDEX IX_hwa_user_id    ON hub_workflow_assignments(user_id);
    CREATE INDEX IX_hwa_workflow_id ON hub_workflow_assignments(workflow_id);
    PRINT '✅ hub_workflow_assignments table created.';
END
ELSE
    PRINT '⚠️  hub_workflow_assignments already exists — skipped.';
GO

-- -------------------------------------------------------------
-- 3. HUB CONVERSATIONS TRACKER
--    Links SQLite conversation UUIDs to SQL Server users so
--    we can track per-user activity without touching the SQLite schema.
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'hub_conversations')
BEGIN
    CREATE TABLE hub_conversations (
        id              INT           IDENTITY(1,1) PRIMARY KEY,
        conversation_id NVARCHAR(36)  NOT NULL UNIQUE,  -- SQLite conversation UUID
        user_id         INT           NOT NULL,
        agent_id        NVARCHAR(36)  NOT NULL,
        agent_name      NVARCHAR(255) NULL,
        created_at      DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_hc_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE INDEX IX_hc_user_id    ON hub_conversations(user_id);
    CREATE INDEX IX_hc_agent_id   ON hub_conversations(agent_id);
    CREATE INDEX IX_hc_created_at ON hub_conversations(created_at DESC);
    PRINT '✅ hub_conversations table created.';
END
ELSE
    PRINT '⚠️  hub_conversations already exists — skipped.';
GO

-- -------------------------------------------------------------
-- 4. HUB WORKFLOW RUNS TRACKER
--    Links SQLite workflow_run UUIDs to SQL Server users.
-- -------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'hub_workflow_runs')
BEGIN
    CREATE TABLE hub_workflow_runs (
        id          INT           IDENTITY(1,1) PRIMARY KEY,
        run_id      NVARCHAR(36)  NOT NULL UNIQUE,   -- SQLite workflow_runs UUID
        user_id     INT           NOT NULL,
        workflow_id NVARCHAR(36)  NOT NULL,
        workflow_name NVARCHAR(255) NULL,
        started_at  DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        CONSTRAINT fk_hwr_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE INDEX IX_hwr_user_id     ON hub_workflow_runs(user_id);
    CREATE INDEX IX_hwr_workflow_id ON hub_workflow_runs(workflow_id);
    PRINT '✅ hub_workflow_runs table created.';
END
ELSE
    PRINT '⚠️  hub_workflow_runs already exists — skipped.';
GO

-- -------------------------------------------------------------
-- 5. ENSURE hub_chat + hub_workflow call types in token_usage
--    The token_usage.call_type column already exists (NVARCHAR 50).
--    New call types used by agents hub:
--      hub_chat      — streaming chat with an orchestrated agent
--      hub_workflow  — workflow execution
--    No schema change needed; call types are free-form strings.
-- -------------------------------------------------------------
PRINT '✅ No schema change needed for token_usage — call types are free-form.';
GO

-- -------------------------------------------------------------
-- 6. HELPFUL VIEWS
-- -------------------------------------------------------------

-- Per-user hub usage today
IF EXISTS (SELECT 1 FROM sys.views WHERE name = 'vw_hub_usage_today')
    DROP VIEW vw_hub_usage_today;
GO
CREATE VIEW vw_hub_usage_today AS
    SELECT u.id   AS user_id,
           u.username,
           r.name AS role,
           ISNULL(SUM(CASE WHEN t.call_type IN ('hub_chat','hub_workflow') THEN t.tokens_used END), 0) AS hub_tokens_today,
           ISNULL(COUNT(CASE WHEN t.call_type = 'hub_chat'     AND CAST(t.used_at AS DATE) = CAST(GETUTCDATE() AS DATE) THEN 1 END), 0) AS hub_chats_today,
           ISNULL(COUNT(CASE WHEN t.call_type = 'hub_workflow' AND CAST(t.used_at AS DATE) = CAST(GETUTCDATE() AS DATE) THEN 1 END), 0) AS hub_workflows_today
    FROM  users u
    JOIN  roles r ON r.id = u.role_id
    LEFT JOIN token_usage t
           ON t.user_id = u.id
          AND CAST(t.used_at AS DATE) = CAST(GETUTCDATE() AS DATE)
    GROUP BY u.id, u.username, r.name;
GO

-- Per-agent assignment summary
IF EXISTS (SELECT 1 FROM sys.views WHERE name = 'vw_hub_agent_assignment_summary')
    DROP VIEW vw_hub_agent_assignment_summary;
GO
CREATE VIEW vw_hub_agent_assignment_summary AS
    SELECT haa.agent_id,
           haa.agent_name,
           COUNT(DISTINCT haa.user_id) AS assigned_users,
           MAX(haa.assigned_at)        AS last_assigned_at
    FROM  hub_agent_assignments haa
    GROUP BY haa.agent_id, haa.agent_name;
GO

PRINT '✅ Agents Hub views created.';
GO

PRINT '=============================================================';
PRINT '✅ Agents Hub integration tables created successfully.';
PRINT '   New tables: hub_agent_assignments, hub_workflow_assignments,';
PRINT '               hub_conversations, hub_workflow_runs';
PRINT '   New views:  vw_hub_usage_today, vw_hub_agent_assignment_summary';
PRINT '   Token call types: hub_chat, hub_workflow (no schema change needed)';
PRINT '=============================================================';
GO
