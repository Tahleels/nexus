-- ============================================================
-- hub_approvals.sql
-- Run in SSMS against the nexus database.
-- Adds the Human-in-the-Loop approval tracking table.
-- ============================================================

CREATE TABLE hub_approvals (
    id                   INT IDENTITY(1,1) PRIMARY KEY,

    -- Unique identifier surfaced to the frontend / agents
    approval_id          NVARCHAR(36)   NOT NULL UNIQUE DEFAULT LOWER(NEWID()),

    -- 'agent'    -> triggered by an agent tool call
    -- 'workflow' -> triggered by a workflow approval node
    request_type         NVARCHAR(20)   NOT NULL DEFAULT 'agent',

    -- Source context (whichever is applicable)
    agent_id             NVARCHAR(36)   NULL,
    agent_name           NVARCHAR(200)  NULL,
    workflow_id          NVARCHAR(36)   NULL,
    workflow_name        NVARCHAR(200)  NULL,
    conversation_id      NVARCHAR(36)   NULL,   -- agent chat conversation UUID
    run_id               NVARCHAR(36)   NULL,   -- workflow run UUID
    node_id              NVARCHAR(100)  NULL,   -- workflow node that requested approval

    -- Parties
    requested_by_user_id INT            NOT NULL,   -- user who triggered the agent/workflow
    assigned_to_user_id  INT            NULL,        -- NULL = any admin can approve

    -- Request content
    title                NVARCHAR(500)  NOT NULL,
    context_json         NVARCHAR(MAX)  NULL,        -- rich JSON with full context

    -- Resolution
    status               NVARCHAR(20)   NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    approver_user_id     INT            NULL,
    approver_note        NVARCHAR(2000) NULL,

    -- Timestamps
    created_at           DATETIME2      DEFAULT GETDATE(),
    resolved_at          DATETIME2      NULL
);

CREATE INDEX ix_hub_approvals_status    ON hub_approvals(status);
CREATE INDEX ix_hub_approvals_assigned  ON hub_approvals(assigned_to_user_id);
CREATE INDEX ix_hub_approvals_requester ON hub_approvals(requested_by_user_id);
CREATE INDEX ix_hub_approvals_convo     ON hub_approvals(conversation_id);
CREATE INDEX ix_hub_approvals_run       ON hub_approvals(run_id);
GO

PRINT 'hub_approvals table created successfully.';
GO
