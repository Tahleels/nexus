-- ================================================================
-- Enterprise AI Workspace — SQL Server table definitions
-- Run once against your nexus database
-- ================================================================

-- ── Workspaces (top-level containers, personal or team) ─────────
CREATE TABLE ws_workspaces (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    owner_id     INT NOT NULL REFERENCES users(id),
    name         NVARCHAR(255)  NOT NULL,
    description  NVARCHAR(1000) NULL,
    color        NVARCHAR(20)   DEFAULT '#6366f1',
    icon         NVARCHAR(50)   DEFAULT 'fa-brain',
    is_team      BIT            DEFAULT 0,
    is_archived  BIT            DEFAULT 0,
    created_at   DATETIME2      DEFAULT GETUTCDATE(),
    updated_at   DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Workspace members (team workspaces) ─────────────────────────
CREATE TABLE ws_workspace_members (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES ws_workspaces(id) ON DELETE CASCADE,
    user_id      INT NOT NULL REFERENCES users(id),
    role         NVARCHAR(20)   DEFAULT 'member',   -- owner|admin|member|viewer
    joined_at    DATETIME2      DEFAULT GETUTCDATE(),
    CONSTRAINT UQ_ws_member UNIQUE (workspace_id, user_id)
);

-- ── Projects (sub-containers within a workspace) ────────────────
CREATE TABLE ws_projects (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    workspace_id  INT NULL REFERENCES ws_workspaces(id),
    owner_id      INT NOT NULL REFERENCES users(id),
    name          NVARCHAR(255)  NOT NULL,
    description   NVARCHAR(1000) NULL,
    system_prompt NVARCHAR(MAX)  NULL,
    model         NVARCHAR(100)  DEFAULT 'gpt-4o',
    provider      NVARCHAR(20)   DEFAULT 'openai',
    temperature   FLOAT          DEFAULT 0.7,
    is_archived   BIT            DEFAULT 0,
    created_at    DATETIME2      DEFAULT GETUTCDATE(),
    updated_at    DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Conversations (chat threads) ────────────────────────────────
CREATE TABLE ws_conversations (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    project_id     INT NULL REFERENCES ws_projects(id),
    workspace_id   INT NULL REFERENCES ws_workspaces(id),
    user_id        INT NOT NULL REFERENCES users(id),
    title          NVARCHAR(500)  NULL,
    model          NVARCHAR(100)  DEFAULT 'gpt-4o',
    provider       NVARCHAR(20)   DEFAULT 'openai',
    system_prompt  NVARCHAR(MAX)  NULL,
    tools_enabled  NVARCHAR(500)  DEFAULT '[]',
    is_starred     BIT            DEFAULT 0,
    is_archived    BIT            DEFAULT 0,
    message_count  INT            DEFAULT 0,
    total_tokens   INT            DEFAULT 0,
    created_at     DATETIME2      DEFAULT GETUTCDATE(),
    updated_at     DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Messages (individual turns in a conversation) ───────────────
CREATE TABLE ws_messages (
    id                INT IDENTITY(1,1) PRIMARY KEY,
    conversation_id   INT NOT NULL REFERENCES ws_conversations(id) ON DELETE CASCADE,
    user_id           INT NOT NULL REFERENCES users(id),
    role              NVARCHAR(20)   NOT NULL,     -- user|assistant|system|tool
    content           NVARCHAR(MAX)  NULL,
    content_type      NVARCHAR(50)   DEFAULT 'text',  -- text|code|sql|image|file
    model             NVARCHAR(100)  NULL,
    finish_reason     NVARCHAR(50)   NULL,
    prompt_tokens     INT            DEFAULT 0,
    completion_tokens INT            DEFAULT 0,
    total_tokens      INT            DEFAULT 0,
    latency_ms        INT            DEFAULT 0,
    is_edited         BIT            DEFAULT 0,
    is_deleted        BIT            DEFAULT 0,
    parent_msg_id     INT            NULL,
    sequence_num      INT            DEFAULT 0,
    created_at        DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Streaming chunks (stored for replay) ────────────────────────
CREATE TABLE ws_message_chunks (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    message_id   INT NOT NULL REFERENCES ws_messages(id) ON DELETE CASCADE,
    chunk_index  INT NOT NULL,
    chunk_text   NVARCHAR(MAX) NULL,
    chunk_type   NVARCHAR(50)  DEFAULT 'text',
    created_at   DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Tool calls executed during a message ────────────────────────
CREATE TABLE ws_tool_calls (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    message_id      INT NOT NULL REFERENCES ws_messages(id) ON DELETE CASCADE,
    conversation_id INT NOT NULL,
    tool_call_id    NVARCHAR(100)  NOT NULL,
    tool_name       NVARCHAR(100)  NOT NULL,
    tool_input      NVARCHAR(MAX)  NULL,
    tool_output     NVARCHAR(MAX)  NULL,
    status          NVARCHAR(20)   DEFAULT 'pending',  -- pending|running|success|error
    latency_ms      INT            DEFAULT 0,
    tokens_used     INT            DEFAULT 0,
    created_at      DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Citations (from web search results) ─────────────────────────
CREATE TABLE ws_citations (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    message_id     INT NOT NULL REFERENCES ws_messages(id) ON DELETE CASCADE,
    url            NVARCHAR(2000) NOT NULL,
    title          NVARCHAR(1000) NULL,
    snippet        NVARCHAR(MAX)  NULL,
    citation_index INT            DEFAULT 0,
    created_at     DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Artifacts (code, SQL, HTML, reports, images, etc.) ──────────
CREATE TABLE ws_artifacts (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    conversation_id INT NULL REFERENCES ws_conversations(id),
    message_id      INT NULL REFERENCES ws_messages(id),
    user_id         INT NOT NULL REFERENCES users(id),
    artifact_type   NVARCHAR(50)   NOT NULL,  -- code|sql|html|markdown|image|report|dashboard|presentation
    title           NVARCHAR(500)  NULL,
    content         NVARCHAR(MAX)  NULL,
    language        NVARCHAR(50)   NULL,
    file_path       NVARCHAR(1000) NULL,
    mime_type       NVARCHAR(100)  NULL,
    byte_size       INT            DEFAULT 0,
    version         INT            DEFAULT 1,
    is_starred      BIT            DEFAULT 0,
    tags            NVARCHAR(500)  NULL,       -- JSON array
    created_at      DATETIME2      DEFAULT GETUTCDATE(),
    updated_at      DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Uploaded/downloaded files ────────────────────────────────────
CREATE TABLE ws_files (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    conversation_id INT NULL REFERENCES ws_conversations(id),
    message_id      INT NULL,
    user_id         INT NOT NULL REFERENCES users(id),
    file_name       NVARCHAR(500)  NOT NULL,
    file_path       NVARCHAR(1000) NOT NULL,
    mime_type       NVARCHAR(100)  NULL,
    byte_size       INT            DEFAULT 0,
    openai_file_id  NVARCHAR(200)  NULL,
    direction       NVARCHAR(10)   DEFAULT 'upload',  -- upload|download
    created_at      DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Prompt library (saved/shared reusable prompts) ──────────────
CREATE TABLE ws_prompt_library (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES users(id),
    workspace_id INT NULL REFERENCES ws_workspaces(id),
    title        NVARCHAR(500) NOT NULL,
    prompt_text  NVARCHAR(MAX) NOT NULL,
    category     NVARCHAR(100) DEFAULT 'general',
    tags         NVARCHAR(500) NULL,
    use_count    INT           DEFAULT 0,
    is_shared    BIT           DEFAULT 0,
    created_at   DATETIME2     DEFAULT GETUTCDATE(),
    updated_at   DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Full replay event log ────────────────────────────────────────
CREATE TABLE ws_replay_events (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    conversation_id INT NOT NULL,
    message_id      INT NULL,
    event_type      NVARCHAR(100) NOT NULL,  -- user_message|chunk|tool_start|tool_done|citation|message_done|error
    event_data      NVARCHAR(MAX) NULL,       -- JSON blob
    sequence_num    INT           NOT NULL,
    created_at      DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Enterprise memory ────────────────────────────────────────────
CREATE TABLE ws_enterprise_memory (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    user_id        INT NOT NULL REFERENCES users(id),
    workspace_id   INT NULL REFERENCES ws_workspaces(id),
    memory_key     NVARCHAR(500) NOT NULL,
    memory_value   NVARCHAR(MAX) NOT NULL,
    source_conv_id INT NULL,
    scope          NVARCHAR(20)  DEFAULT 'user',  -- user|workspace|enterprise
    importance     INT           DEFAULT 5,        -- 1-10
    is_active      BIT           DEFAULT 1,
    created_at     DATETIME2     DEFAULT GETUTCDATE(),
    updated_at     DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Message feedback (thumbs up / down) ─────────────────────────
CREATE TABLE ws_feedback (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    message_id INT NOT NULL REFERENCES ws_messages(id) ON DELETE CASCADE,
    user_id    INT NOT NULL REFERENCES users(id),
    rating     NVARCHAR(10)   NOT NULL,  -- thumbs_up|thumbs_down
    comment    NVARCHAR(1000) NULL,
    created_at DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Message edit history ─────────────────────────────────────────
CREATE TABLE ws_message_edits (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    message_id  INT NOT NULL REFERENCES ws_messages(id) ON DELETE CASCADE,
    user_id     INT NOT NULL REFERENCES users(id),
    old_content NVARCHAR(MAX) NULL,
    new_content NVARCHAR(MAX) NULL,
    edited_at   DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Copy/share tracking ──────────────────────────────────────────
CREATE TABLE ws_copy_log (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    message_id INT NOT NULL,
    user_id    INT NOT NULL REFERENCES users(id),
    copy_type  NVARCHAR(50) DEFAULT 'text',  -- text|code|markdown
    created_at DATETIME2    DEFAULT GETUTCDATE()
);

-- ── Retry log ────────────────────────────────────────────────────
CREATE TABLE ws_retry_log (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    conversation_id INT NOT NULL,
    message_id      INT NOT NULL,
    user_id         INT NOT NULL REFERENCES users(id),
    attempt_num     INT NOT NULL,
    error_msg       NVARCHAR(1000) NULL,
    created_at      DATETIME2      DEFAULT GETUTCDATE()
);

-- ── Workspace activity log ───────────────────────────────────────
CREATE TABLE ws_activity_log (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id),
    action      NVARCHAR(100) NOT NULL,
    entity_type NVARCHAR(50)  NULL,   -- conversation|project|workspace|artifact|prompt
    entity_id   INT           NULL,
    details     NVARCHAR(MAX) NULL,   -- JSON blob
    ip_address  NVARCHAR(45)  NULL,
    created_at  DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Per-model token and cost tracking ───────────────────────────
CREATE TABLE ws_model_usage (
    id                  INT IDENTITY(1,1) PRIMARY KEY,
    user_id             INT NOT NULL REFERENCES users(id),
    model               NVARCHAR(100) NOT NULL,
    conversation_id     INT NULL,
    prompt_tokens       INT           DEFAULT 0,
    completion_tokens   INT           DEFAULT 0,
    total_tokens        INT           DEFAULT 0,
    estimated_cost_usd  DECIMAL(10,6) DEFAULT 0,
    call_type           NVARCHAR(50)  DEFAULT 'chat',
    created_at          DATETIME2     DEFAULT GETUTCDATE()
);

-- ── Workspace settings (per-user key/value store) ───────────────
CREATE TABLE ws_user_settings (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id),
    setting_key   NVARCHAR(200) NOT NULL,
    setting_value NVARCHAR(MAX) NULL,
    updated_at    DATETIME2     DEFAULT GETUTCDATE(),
    CONSTRAINT UQ_ws_user_setting UNIQUE (user_id, setting_key)
);

-- ── Performance indexes ──────────────────────────────────────────
CREATE INDEX IX_ws_conversations_user    ON ws_conversations   (user_id, created_at DESC);
CREATE INDEX IX_ws_conversations_proj    ON ws_conversations   (project_id);
CREATE INDEX IX_ws_messages_conv         ON ws_messages        (conversation_id, sequence_num);
CREATE INDEX IX_ws_replay_conv           ON ws_replay_events   (conversation_id, sequence_num);
CREATE INDEX IX_ws_artifacts_user        ON ws_artifacts       (user_id, artifact_type, created_at DESC);
CREATE INDEX IX_ws_model_usage_user      ON ws_model_usage     (user_id, created_at DESC);
CREATE INDEX IX_ws_activity_user         ON ws_activity_log    (user_id, created_at DESC);
CREATE INDEX IX_ws_prompt_user           ON ws_prompt_library  (user_id, category);
CREATE INDEX IX_ws_memory_user           ON ws_enterprise_memory (user_id, scope);
CREATE INDEX IX_ws_chunks_msg            ON ws_message_chunks  (message_id, chunk_index);
