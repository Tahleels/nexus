-- Migration: add `provider` alongside the existing `model` column so hub
-- agents and workspace projects/conversations can select Amazon Bedrock (or
-- any future provider) as well as OpenAI. See llm_providers/ for the
-- provider-agnostic chat abstraction that reads this column.
-- Safe to re-run.

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('hub_agents') AND name='provider')
    ALTER TABLE hub_agents ADD provider NVARCHAR(20) NOT NULL DEFAULT 'openai';
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('ws_projects') AND name='provider')
    ALTER TABLE ws_projects ADD provider NVARCHAR(20) DEFAULT 'openai';
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('ws_conversations') AND name='provider')
    ALTER TABLE ws_conversations ADD provider NVARCHAR(20) DEFAULT 'openai';
GO

PRINT 'Migration applied: provider columns added to hub_agents, ws_projects, ws_conversations.';
