-- Migration: extend hub_jobs for tool-type jobs
-- Safe to re-run

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='tool_name')
    ALTER TABLE hub_jobs ADD tool_name NVARCHAR(100) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='tool_params_json')
    ALTER TABLE hub_jobs ADD tool_params_json NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='tool_env_vars_json')
    ALTER TABLE hub_jobs ADD tool_env_vars_json NVARCHAR(MAX) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='run_count')
    ALTER TABLE hub_jobs ADD run_count INT NOT NULL DEFAULT 0;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='last_run_at')
    ALTER TABLE hub_jobs ADD last_run_at DATETIME2 NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='last_run_status')
    ALTER TABLE hub_jobs ADD last_run_status NVARCHAR(20) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_jobs') AND name='last_run_output')
    ALTER TABLE hub_jobs ADD last_run_output NVARCHAR(MAX) NULL;
GO

PRINT 'Migration applied: tool job columns added to hub_jobs.';
