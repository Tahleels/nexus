-- =============================================================================
-- TRAINING DATA MODULE — SQL Server DDL
-- Run once in SSMS against nexus database
-- Requires workspace_tables.sql to have been run first (ws_messages, ws_conversations, etc.)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. ws_training_pairs
--    One row per curated instruction/output pair ready for SLM training
-- -----------------------------------------------------------------------------
IF OBJECT_ID('ws_training_pairs', 'U') IS NULL
CREATE TABLE ws_training_pairs (
    id                  INT            IDENTITY(1,1) PRIMARY KEY,

    -- Source traceability
    message_id          INT            NULL REFERENCES ws_messages(id) ON DELETE NO ACTION,
    conversation_id     INT            NULL REFERENCES ws_conversations(id) ON DELETE NO ACTION,
    workspace_id        INT            NULL REFERENCES ws_workspaces(id) ON DELETE NO ACTION,

    -- The actual training content
    instruction         NVARCHAR(MAX)  NOT NULL,   -- user prompt / question
    context             NVARCHAR(MAX)  NULL,        -- optional system / prior context injected
    output              NVARCHAR(MAX)  NOT NULL,   -- model response (ground truth)

    -- Quality & review
    quality_score       FLOAT          NOT NULL DEFAULT 50.0,  -- 0..100
    quality_breakdown   NVARCHAR(MAX)  NULL,        -- JSON blob of score components
    status              NVARCHAR(20)   NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','auto_approved','approved','rejected','needs_review')),
    reviewed_by         INT            NULL REFERENCES users(id) ON DELETE NO ACTION,
    reviewed_at         DATETIME       NULL,
    review_notes        NVARCHAR(1000) NULL,

    -- Taxonomy
    domain              NVARCHAR(100)  NULL,   -- e.g. 'sales', 'support', 'technical'
    tags                NVARCHAR(500)  NULL,   -- comma-separated
    language            NVARCHAR(10)   NOT NULL DEFAULT 'en',

    -- Metadata
    model_used          NVARCHAR(100)  NULL,
    token_count         INT            NULL,
    has_citations       BIT            NOT NULL DEFAULT 0,
    has_artifact        BIT            NOT NULL DEFAULT 0,
    is_edited           BIT            NOT NULL DEFAULT 0,
    feedback_rating     NVARCHAR(20)   NULL,   -- thumbs_up / thumbs_down / NULL
    retry_count         INT            NOT NULL DEFAULT 0,

    created_at          DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    updated_at          DATETIME       NOT NULL DEFAULT GETUTCDATE()
);

-- -----------------------------------------------------------------------------
-- 2. ws_training_preference_pairs
--    DPO / RLHF-style pairs: same prompt, one chosen response, one rejected
-- -----------------------------------------------------------------------------
IF OBJECT_ID('ws_training_preference_pairs', 'U') IS NULL
CREATE TABLE ws_training_preference_pairs (
    id                  INT            IDENTITY(1,1) PRIMARY KEY,

    -- Can be derived from a base training pair
    base_pair_id        INT            NULL REFERENCES ws_training_pairs(id) ON DELETE NO ACTION,
    conversation_id     INT            NULL REFERENCES ws_conversations(id) ON DELETE NO ACTION,

    prompt              NVARCHAR(MAX)  NOT NULL,
    chosen_output       NVARCHAR(MAX)  NOT NULL,   -- preferred / approved response
    rejected_output     NVARCHAR(MAX)  NOT NULL,   -- dispreferred / edited-away response

    chosen_score        FLOAT          NULL,
    rejected_score      FLOAT          NULL,

    -- Source of the preference signal
    preference_source   NVARCHAR(50)   NOT NULL DEFAULT 'human_feedback'
                            CHECK (preference_source IN ('human_feedback','edit_pair','retry_pair','annotation')),

    domain              NVARCHAR(100)  NULL,
    tags                NVARCHAR(500)  NULL,
    status              NVARCHAR(20)   NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','approved','rejected')),
    reviewed_by         INT            NULL REFERENCES users(id) ON DELETE NO ACTION,
    reviewed_at         DATETIME       NULL,

    created_at          DATETIME       NOT NULL DEFAULT GETUTCDATE()
);

-- -----------------------------------------------------------------------------
-- 3. ws_training_exports
--    Audit log of every export: what format, how many pairs, who exported
-- -----------------------------------------------------------------------------
IF OBJECT_ID('ws_training_exports', 'U') IS NULL
CREATE TABLE ws_training_exports (
    id                  INT            IDENTITY(1,1) PRIMARY KEY,
    exported_by         INT            NOT NULL REFERENCES users(id),

    export_format       NVARCHAR(30)   NOT NULL
                            CHECK (export_format IN ('alpaca_jsonl','chatml_jsonl','openai_ft_jsonl','dpo_jsonl')),
    pair_count          INT            NOT NULL DEFAULT 0,
    preference_count    INT            NOT NULL DEFAULT 0,

    -- Filters applied at export time (stored as JSON for auditability)
    filters             NVARCHAR(MAX)  NULL,   -- {"status":"approved","domain":"sales",...}

    file_name           NVARCHAR(500)  NULL,
    file_size_bytes     INT            NULL,

    -- The actual JSONL content is streamed to the client; we store a hash for dedup
    content_hash        NVARCHAR(64)   NULL,

    exported_at         DATETIME       NOT NULL DEFAULT GETUTCDATE()
);

-- -----------------------------------------------------------------------------
-- 4. ws_training_annotations
--    Fine-grained human annotations on individual pairs (optional, future use)
-- -----------------------------------------------------------------------------
IF OBJECT_ID('ws_training_annotations', 'U') IS NULL
CREATE TABLE ws_training_annotations (
    id                  INT            IDENTITY(1,1) PRIMARY KEY,
    pair_id             INT            NOT NULL REFERENCES ws_training_pairs(id) ON DELETE NO ACTION,
    annotated_by        INT            NOT NULL REFERENCES users(id) ON DELETE NO ACTION,

    annotation_type     NVARCHAR(50)   NOT NULL   -- 'factual_error','hallucination','tone','length','other'
                            CHECK (annotation_type IN ('factual_error','hallucination','tone','length','format','other')),
    annotation_text     NVARCHAR(2000) NULL,
    severity            NVARCHAR(20)   NOT NULL DEFAULT 'minor'
                            CHECK (severity IN ('minor','major','critical')),

    created_at          DATETIME       NOT NULL DEFAULT GETUTCDATE()
);

-- =============================================================================
-- INDEXES
-- =============================================================================

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_pairs_status')
    CREATE INDEX IX_training_pairs_status       ON ws_training_pairs (status);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_pairs_quality')
    CREATE INDEX IX_training_pairs_quality      ON ws_training_pairs (quality_score DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_pairs_domain')
    CREATE INDEX IX_training_pairs_domain       ON ws_training_pairs (domain);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_pairs_message')
    CREATE INDEX IX_training_pairs_message      ON ws_training_pairs (message_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_pairs_conv')
    CREATE INDEX IX_training_pairs_conv         ON ws_training_pairs (conversation_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_pref_base')
    CREATE INDEX IX_training_pref_base          ON ws_training_preference_pairs (base_pair_id);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_exports_by')
    CREATE INDEX IX_training_exports_by         ON ws_training_exports (exported_by, exported_at DESC);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_training_annotations_pair')
    CREATE INDEX IX_training_annotations_pair   ON ws_training_annotations (pair_id);

-- =============================================================================
-- DONE — verify with:
--   SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
--   WHERE TABLE_NAME LIKE 'ws_training%'
-- =============================================================================
