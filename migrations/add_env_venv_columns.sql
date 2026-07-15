-- Migration: add env_vars_json to hub_agents and hub_custom_tools, venv_path to hub_custom_tools
-- Safe to re-run: uses IF NOT EXISTS guards

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_agents') AND name = 'env_vars_json'
)
    ALTER TABLE hub_agents ADD env_vars_json NVARCHAR(MAX) NULL DEFAULT '{}';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_custom_tools') AND name = 'env_vars_json'
)
    ALTER TABLE hub_custom_tools ADD env_vars_json NVARCHAR(MAX) NULL DEFAULT '{}';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('hub_custom_tools') AND name = 'venv_path'
)
    ALTER TABLE hub_custom_tools ADD venv_path NVARCHAR(500) NULL;
GO

PRINT 'Migration applied: env_vars_json + venv_path columns added.';
