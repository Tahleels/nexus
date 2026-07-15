-- =============================================================
-- Migration: add_model_to_token_usage.sql
-- Reference only — this now runs automatically on app startup via
-- database/app_db.py's ensure_tables() (_TABLE_STMTS list), which
-- guards each ALTER with the same IF NOT EXISTS check. You do not
-- need to run this file manually; it's kept for manual/DBA use.
--
-- Why: token_usage previously had no `model` column, so
-- get_today_cost() had to assume every call was "gpt-4o" when
-- computing cost from input_tokens/output_tokens. Any user whose
-- calls actually ran on gpt-4o-mini (or another model) got a
-- wildly inaccurate cost estimate on their quota page. Recording
-- the real model per call lets cost be computed with the correct
-- per-model rate.
--
-- agent_id: a stable reference (hub_agents.id) for hub_chat/
-- hub_workflow rows, so analytics can join on ID instead of the
-- mutable agent_name string — a renamed hub agent would otherwise
-- lose its historical usage from any name-keyed join.
-- =============================================================

USE [nexus];
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('token_usage') AND name = 'model'
)
BEGIN
    ALTER TABLE token_usage ADD model NVARCHAR(100) NULL;
    PRINT '✅ token_usage.model added.';
END
ELSE
    PRINT '⚠️  token_usage.model already exists — skipped.';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('token_usage') AND name = 'agent_id'
)
BEGIN
    ALTER TABLE token_usage ADD agent_id NVARCHAR(36) NULL;
    PRINT '✅ token_usage.agent_id added.';
END
ELSE
    PRINT '⚠️  token_usage.agent_id already exists — skipped.';
GO
