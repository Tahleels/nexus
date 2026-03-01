-- =============================================================================
-- BI TOOL TRAINING DATA — SQL Server DDL
-- Run once in SSMS against nexus database
-- Captures instruction/output pairs from every BI tool (query, dashboard,
-- report, infographic, presentation, optimize) for SLM fine-tuning.
-- =============================================================================

IF OBJECT_ID('bi_training_pairs', 'U') IS NULL
CREATE TABLE bi_training_pairs (
    id                  INT            IDENTITY(1,1) PRIMARY KEY,

    -- Which BI tool generated this pair
    tool_type           NVARCHAR(50)   NOT NULL
                            CHECK (tool_type IN (
                                'bi_query',
                                'bi_dashboard',
                                'bi_report',
                                'bi_infographic',
                                'bi_presentation',
                                'bi_presentation_optimize'
                            )),

    -- Source user (nullable — admin-run tasks may not have a user)
    user_id             INT            NULL REFERENCES users(id) ON DELETE NO ACTION,

    -- Agent / client context
    agent_name          NVARCHAR(200)  NULL,

    -- Core training content
    instruction         NVARCHAR(MAX)  NOT NULL,   -- NL question / request
    context             NVARCHAR(MAX)  NULL,        -- schema / client / summary context
    output              NVARCHAR(MAX)  NOT NULL,   -- generated result (JSON)
    sql_query           NVARCHAR(MAX)  NULL,        -- generated SQL (bi_query only)

    -- Quality & review
    quality_score       FLOAT          NOT NULL DEFAULT 60.0,
    quality_breakdown   NVARCHAR(MAX)  NULL,        -- JSON blob
    status              NVARCHAR(20)   NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','auto_approved','approved','rejected','needs_review')),
    reviewed_by         INT            NULL REFERENCES users(id) ON DELETE NO ACTION,
    reviewed_at         DATETIME       NULL,
    review_notes        NVARCHAR(1000) NULL,

    -- Metadata
    model_used          NVARCHAR(100)  NULL,
    token_count         INT            NULL,
    feedback_rating     NVARCHAR(20)   NULL,   -- thumbs_up / thumbs_down / NULL
    domain              NVARCHAR(100)  NULL,
    tags                NVARCHAR(500)  NULL,

    created_at          DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    updated_at          DATETIME       NOT NULL DEFAULT GETUTCDATE()
);

-- =============================================================================
-- INDEXES
-- =============================================================================

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bi_training_tool_type')
    CREATE INDEX IX_bi_training_tool_type  ON bi_training_pairs (tool_type);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bi_training_status')
    CREATE INDEX IX_bi_training_status     ON bi_training_pairs (status);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bi_training_quality')
    CREATE INDEX IX_bi_training_quality    ON bi_training_pairs (quality_score DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bi_training_user')
    CREATE INDEX IX_bi_training_user       ON bi_training_pairs (user_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bi_training_agent')
    CREATE INDEX IX_bi_training_agent      ON bi_training_pairs (agent_name);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bi_training_created')
    CREATE INDEX IX_bi_training_created    ON bi_training_pairs (created_at DESC);

-- =============================================================================
-- DONE — verify with:
--   SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
--   WHERE TABLE_NAME = 'bi_training_pairs'
-- =============================================================================
