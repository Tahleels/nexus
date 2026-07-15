-- Add notes column to ws_projects for persistent project research context
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'ws_projects' AND COLUMN_NAME = 'notes'
)
BEGIN
    ALTER TABLE ws_projects ADD notes NVARCHAR(MAX) NULL;
END
