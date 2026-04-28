-- Migration 001: Workspace tool config + memory targeting
-- Run once against nexus database

-- 1. Add tools_enabled column to ws_projects (stores JSON array of extra tool names)
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'ws_projects' AND COLUMN_NAME = 'tools_enabled'
)
BEGIN
    ALTER TABLE ws_projects ADD tools_enabled NVARCHAR(MAX) DEFAULT '[]';
    PRINT 'Added ws_projects.tools_enabled';
END

-- 2. Add target_user_id to ws_enterprise_memory (admin can assign memory to specific users)
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'ws_enterprise_memory' AND COLUMN_NAME = 'target_user_id'
)
BEGIN
    ALTER TABLE ws_enterprise_memory ADD target_user_id INT NULL REFERENCES users(id);
    PRINT 'Added ws_enterprise_memory.target_user_id';
END

-- 3. Index for fast lookups of memories targeting a specific user
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ws_memory_target_user')
BEGIN
    CREATE INDEX IX_ws_memory_target_user ON ws_enterprise_memory (target_user_id)
    WHERE target_user_id IS NOT NULL;
    PRINT 'Created IX_ws_memory_target_user';
END
