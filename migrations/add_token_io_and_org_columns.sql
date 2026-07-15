-- =============================================================
-- Migration: add_token_io_and_org_columns.sql
-- Run once in SSMS against the nexus database.
-- Safe to re-run: every ALTER is guarded by IF NOT EXISTS.
-- =============================================================

USE [nexus];
GO

-- ── 1. token_usage — add input_tokens and output_tokens ───────────────────────
--    These columns were missing; all per-call cost calculations depend on them.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('token_usage') AND name = 'input_tokens'
)
BEGIN
    ALTER TABLE token_usage ADD input_tokens INT NOT NULL DEFAULT 0;
    PRINT '✅ token_usage.input_tokens added.';
END
ELSE
    PRINT '⚠️  token_usage.input_tokens already exists — skipped.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('token_usage') AND name = 'output_tokens'
)
BEGIN
    ALTER TABLE token_usage ADD output_tokens INT NOT NULL DEFAULT 0;
    PRINT '✅ token_usage.output_tokens added.';
END
ELSE
    PRINT '⚠️  token_usage.output_tokens already exists — skipped.';
GO

-- ── 2. hub_messages — add input_tokens and output_tokens ─────────────────────
--    Assistant messages are inserted with these values; they were missing from the schema.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_messages') AND name = 'input_tokens'
)
BEGIN
    ALTER TABLE hub_messages ADD input_tokens INT NOT NULL DEFAULT 0;
    PRINT '✅ hub_messages.input_tokens added.';
END
ELSE
    PRINT '⚠️  hub_messages.input_tokens already exists — skipped.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_messages') AND name = 'output_tokens'
)
BEGIN
    ALTER TABLE hub_messages ADD output_tokens INT NOT NULL DEFAULT 0;
    PRINT '✅ hub_messages.output_tokens added.';
END
ELSE
    PRINT '⚠️  hub_messages.output_tokens already exists — skipped.';
GO

-- ── 3. hub_agents — add department_id, project_id, created_by ────────────────
--    Org system columns used in INSERT and analytics JOIN filters.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_agents') AND name = 'department_id'
)
BEGIN
    ALTER TABLE hub_agents ADD department_id INT NULL;
    CREATE INDEX ix_hub_agents_dept ON hub_agents(department_id);
    PRINT '✅ hub_agents.department_id added.';
END
ELSE
    PRINT '⚠️  hub_agents.department_id already exists — skipped.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_agents') AND name = 'project_id'
)
BEGIN
    ALTER TABLE hub_agents ADD project_id INT NULL;
    CREATE INDEX ix_hub_agents_proj ON hub_agents(project_id);
    PRINT '✅ hub_agents.project_id added.';
END
ELSE
    PRINT '⚠️  hub_agents.project_id already exists — skipped.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_agents') AND name = 'created_by'
)
BEGIN
    ALTER TABLE hub_agents ADD created_by INT NULL;
    PRINT '✅ hub_agents.created_by added.';
END
ELSE
    PRINT '⚠️  hub_agents.created_by already exists — skipped.';
GO

-- ── 4. hub_workflows — add department_id and project_id ───────────────────────
--    Org system columns used in INSERT and analytics JOIN filters.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_workflows') AND name = 'department_id'
)
BEGIN
    ALTER TABLE hub_workflows ADD department_id INT NULL;
    CREATE INDEX ix_hub_workflows_dept ON hub_workflows(department_id);
    PRINT '✅ hub_workflows.department_id added.';
END
ELSE
    PRINT '⚠️  hub_workflows.department_id already exists — skipped.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_workflows') AND name = 'project_id'
)
BEGIN
    ALTER TABLE hub_workflows ADD project_id INT NULL;
    CREATE INDEX ix_hub_workflows_proj ON hub_workflows(project_id);
    PRINT '✅ hub_workflows.project_id added.';
END
ELSE
    PRINT '⚠️  hub_workflows.project_id already exists — skipped.';
GO

PRINT '=============================================================';
PRINT '✅ Migration complete.';
PRINT '   token_usage:   + input_tokens, output_tokens';
PRINT '   hub_messages:  + input_tokens, output_tokens';
PRINT '   hub_agents:    + department_id, project_id, created_by';
PRINT '   hub_workflows: + department_id, project_id';
PRINT '=============================================================';
GO
