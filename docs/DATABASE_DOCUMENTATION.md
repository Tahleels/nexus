# Database Documentation

**Platform:** SQL Server (Azure SQL / SQL Server 2019+)  
**Total Tables:** ~79  
**Architecture:** App-internal tables created via Python (`database/app_db.py`), workspace/training/hub features via SQL migration scripts in `sql/`

---

## Table of Contents

1. [Authentication & Users](#1-authentication--users)
2. [Organizational Structure](#2-organizational-structure)
3. [Agent System (BI Agents)](#3-agent-system-bi-agents)
4. [Jobs & Scheduling](#4-jobs--scheduling)
5. [Knowledge & Document Store](#5-knowledge--document-store)
6. [Document Watching & Connectors](#6-document-watching--connectors)
7. [NLQ & Schema Intelligence](#7-nlq--schema-intelligence)
8. [Observability & Tracing](#8-observability--tracing)
9. [Hub Agent System](#9-hub-agent-system)
10. [Hub Workflows](#10-hub-workflows)
11. [Hub Approvals (HITL)](#11-hub-approvals-hitl)
12. [Hub Custom Tools](#12-hub-custom-tools)
13. [Workspace (AI Workspace)](#13-workspace-ai-workspace)
14. [Training Data](#14-training-data)
15. [Client Management](#15-client-management)
16. [Relationship Diagram (ERD Summary)](#16-relationship-diagram-erd-summary)

---

## 1. Authentication & Users

Defined in: `sql/auth_setup.sql`

### `roles`
Stores the three system-level roles.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| name | NVARCHAR(50) UNIQUE | `admin`, `dev`, `user` |
| description | NVARCHAR(255) | |
| created_at | DATETIME2 | UTC default |

---

### `users`
Central user table. Every other table with a `user_id` or `created_by` column references this.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| username | NVARCHAR(100) UNIQUE | |
| email | NVARCHAR(255) UNIQUE | |
| password_hash | NVARCHAR(255) | bcrypt |
| role_id | INT FK → roles.id | |
| is_active | BIT | Default 1 |
| created_by | INT FK → users.id | self-ref, null for first admin |
| created_at | DATETIME2 | |
| last_login | DATETIME2 | |

**Indexes:** `IX_users_username`, `IX_users_email`

---

### `user_sessions`
Active login tokens for session-based auth.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id CASCADE | |
| token | NVARCHAR(255) UNIQUE | |
| ip_address | NVARCHAR(45) | IPv6-safe |
| user_agent | NVARCHAR(512) | |
| created_at | DATETIME2 | |
| expires_at | DATETIME2 | |
| is_active | BIT | |

**Indexes:** `IX_sessions_token`, `IX_sessions_user_id`

---

### `token_usage`
Tracks LLM token consumption per user per call for billing/quota.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id CASCADE | |
| used_at | DATETIME2 | |
| tokens_used | INT | |
| agent_name | NVARCHAR(255) | which BI agent |
| question | NVARCHAR(1000) | truncated prompt |
| call_type | NVARCHAR(50) | `chat`, `embed`, etc. |

**Indexes:** `IX_tu_user_date (user_id, used_at)`, `IX_tu_call_type`

---

### `agent_assignments`
Controls which BI agents (by name) a user can access.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id CASCADE | |
| agent_name | NVARCHAR(255) | matches `app_agents.name` |
| assigned_by | INT FK → users.id | |
| assigned_at | DATETIME2 | |

**Constraint:** `uq_aa_user_agent UNIQUE (user_id, agent_name)`  
**Indexes:** `IX_aa_user_id`, `IX_aa_agent_name`

---

## 2. Organizational Structure

Defined in: `database/app_db.py`, `database/org_db.py`

### `departments`
Top-level org grouping for users, agents, jobs, and hub items.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| name | NVARCHAR(255) UNIQUE | |
| description | NVARCHAR(MAX) | |
| color | NVARCHAR(20) | UI hex color, default `#6366f1` |
| created_by | INT | user id |
| created_at | DATETIME2 | |

---

### `projects`
Sub-grouping under departments (logical project scope).

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| name | NVARCHAR(255) | |
| description | NVARCHAR(MAX) | |
| color | NVARCHAR(20) | default `#0ea5e9` |
| created_by | INT | user id |
| created_at | DATETIME2 | |

---

### `user_departments`
Many-to-many: users ↔ departments.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT | FK → users.id |
| dept_id | INT | FK → departments.id |
| assigned_by | INT | |
| assigned_at | DATETIME2 | |
| role | NVARCHAR(50) | `member`, `lead`, etc. |

**Constraint:** `uq_user_dept UNIQUE (user_id, dept_id)`

---

### `user_projects`
Many-to-many: users ↔ projects.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT | FK → users.id |
| project_id | INT | FK → projects.id |
| assigned_by | INT | |
| assigned_at | DATETIME2 | |
| role | NVARCHAR(50) | |

**Constraint:** `uq_user_project UNIQUE (user_id, project_id)`

---

### `agent_guardrails`
Per-scope, per-agent access restrictions — limits which tables/columns can be queried through an agent.
Scope can be a single `user`, a `department`, or a `project`. When a user chats with a BI agent, the
effective guardrail is the **merge** of their own user-scoped guardrail plus any guardrails configured
on the department(s)/project(s) they belong to (`org_db.get_agent_guardrail`).

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT NULL | populated only for `scope_type='user'` rows (legacy/back-compat) |
| scope_type | NVARCHAR(20) | `user` \| `department` \| `project` |
| scope_id | INT | id of the user/department/project this guardrail applies to |
| agent_id | NVARCHAR(255) | agent name or hub UUID |
| agent_type | NVARCHAR(10) | `bi` or `hub` |
| filter_rules | NVARCHAR(MAX) | JSON rule set |
| restrict_tables | NVARCHAR(MAX) | JSON array of allowed tables |
| custom_instruction | NVARCHAR(MAX) | system prompt override |
| created_by | INT | |
| created_at / updated_at | DATETIME2 | |

**Constraint:** `uq_agent_guardrail_scope UNIQUE (scope_type, scope_id, agent_id, agent_type)`

**Merge rules** (`org_db._merge_guardrails`):
- `filter_rules`: union of all rules across applicable scopes (every rule is mandatory/AND-ed).
- `restrict_tables`: intersection across scopes that define it (most restrictive wins).
- `custom_instruction`: all non-empty instructions concatenated.

---

### `dev_proxy_assignments`
Allows a `dev` role user to act on behalf of another user (proxy impersonation for testing).

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| dev_user_id | INT | FK → users.id |
| target_user_id | INT | FK → users.id |
| assigned_by | INT | |
| assigned_at | DATETIME2 | |

**Constraint:** `uq_dev_proxy UNIQUE (dev_user_id, target_user_id)`

---

### `dev_resource_assignments`
Grants a `dev` user explicit access to a specific resource (agent, job, workflow, etc.) regardless of org assignments.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| dev_user_id | INT | |
| resource_type | NVARCHAR(50) | `agent`, `job`, `workflow`, etc. |
| resource_id | INT | id of the resource |
| assigned_by | INT | |
| assigned_at | DATETIME2 | |

**Constraint:** `uq_dev_resource UNIQUE (dev_user_id, resource_type, resource_id)`

---

## 3. Agent System (BI Agents)

Defined in: `database/app_db.py`

### `app_database_connections`
Stores connection credentials for external databases that BI agents query.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| name | NVARCHAR(255) UNIQUE | display name |
| type | NVARCHAR(50) | `sqlserver`, `mysql`, `postgres`, etc. |
| server | NVARCHAR(255) | |
| port | NVARCHAR(20) | |
| username | NVARCHAR(255) | |
| password | NVARCHAR(500) | encrypted at rest |
| db_name | NVARCHAR(255) | |
| status | NVARCHAR(50) | `connected`, `disconnected`, `error` |
| created_at | DATETIME2 | |

---

### `app_agents`
BI agent definitions — each agent is wired to a database connection with a scoped set of tables.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| name | NVARCHAR(255) UNIQUE | used as FK by many tables |
| description | NVARCHAR(MAX) | |
| database_connection | NVARCHAR(255) | FK → app_database_connections.name |
| selected_tables | NVARCHAR(MAX) | JSON array |
| selected_columns | NVARCHAR(MAX) | JSON object (table→columns) |
| schema_context | NVARCHAR(MAX) | pre-built schema description for LLM |
| department_id | INT | FK → departments.id |
| project_id | INT | FK → projects.id |
| created_by | INT | |
| created_at | DATETIME2 | |

---

## 4. Jobs & Scheduling

Defined in: `database/app_db.py`

### `app_jobs`
Scheduled BI tasks — runs an NLQ prompt against an agent on a schedule and delivers output.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| name | NVARCHAR(255) UNIQUE | |
| description | NVARCHAR(MAX) | |
| agent_name | NVARCHAR(255) | FK → app_agents.name |
| nlq_prompt | NVARCHAR(MAX) | the question to run |
| tools | NVARCHAR(MAX) | JSON — enabled tool list |
| schedule | NVARCHAR(MAX) | JSON cron config |
| delivery | NVARCHAR(MAX) | JSON — email/teams delivery config |
| enabled | BIT | |
| created_by | INT | user id |
| created_by_username | NVARCHAR(255) | denormalized |
| created_by_role | NVARCHAR(50) | denormalized |
| created_at / updated_at | DATETIME2 | |
| last_run / next_run | DATETIME2 | |
| run_count | INT | |
| last_status | NVARCHAR(50) | `success`, `failed`, `running` |
| department_id | INT | FK → departments.id |
| project_id | INT | FK → projects.id |

---

### `app_job_executions`
Execution log for every run of a job.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| exec_id | NVARCHAR(36) | UUID for this run |
| job_id | NVARCHAR(36) | FK → app_jobs.id |
| job_name | NVARCHAR(255) | denormalized |
| triggered_by | NVARCHAR(50) | `scheduler`, `manual`, `api` |
| status | NVARCHAR(50) | `running`, `success`, `failed` |
| started_at | DATETIME2 | |
| finished_at | DATETIME2 | |
| duration_ms | INT | |
| error | NVARCHAR(MAX) | |
| tools_completed | NVARCHAR(MAX) | JSON list |
| row_count | INT | rows returned |
| artifacts | NVARCHAR(MAX) | JSON — file paths generated |
| query_result | NVARCHAR(MAX) | final answer |
| log_lines | NVARCHAR(MAX) | streaming log |

**Constraint:** `uq_app_job_exec UNIQUE (job_id, exec_id)`

---

## 5. Knowledge & Document Store

Defined in: `database/app_db.py`

### `app_documents`
Metadata record for every ingested document (file, SharePoint, manual upload).

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(64) PK | content-hash-based |
| filename | NVARCHAR(500) | |
| file_type | NVARCHAR(50) | `pdf`, `docx`, `xlsx`, etc. |
| client_id | NVARCHAR(255) | optional client scoping |
| source_name | NVARCHAR(500) | original path/URL |
| chunk_count | INT | number of vector chunks |
| file_hash | NVARCHAR(32) | MD5 for dedup |
| extra_meta | NVARCHAR(MAX) | JSON extra metadata |
| uploaded_by_id | INT | FK → users.id |
| uploaded_by_name | NVARCHAR(255) | denormalized |
| version | INT | increments on re-upload |
| parent_doc_id | NVARCHAR(64) | FK → app_documents.id (versioning) |
| status | NVARCHAR(50) | `ready`, `processing`, `error` |
| source_watch_id | INT | FK → document_watch_dirs.id or sharepoint_watch_configs.id |
| source_watch_type | NVARCHAR(20) | `folder` or `sharepoint` |
| scope | NVARCHAR(20) | `user`, `department`, `project`, `global` |
| scope_id | INT | id matching the scope type |
| created_at | DATETIME2 | |

---

### `app_document_chunks`
Individual vector chunks from each document. Embeddings stored as JSON-serialized float arrays.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(100) PK | `{doc_id}_chunk_{n}` |
| document_id | NVARCHAR(64) | FK → app_documents.id |
| chunk_index | INT | position in document |
| text_content | NVARCHAR(MAX) | raw text of chunk |
| embedding | NVARCHAR(MAX) | JSON float array |
| filename | NVARCHAR(500) | denormalized |
| file_type | NVARCHAR(50) | denormalized |
| client_id | NVARCHAR(255) | denormalized for fast filter |
| source_name | NVARCHAR(500) | denormalized |
| created_at | DATETIME2 | |

**Indexes:** `ix_doc_chunks_document_id`, `ix_doc_chunks_client_id`

---

### `app_document_assignments`
Controls which users have access to a document (when scope = `user`).

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| doc_id | NVARCHAR(64) | FK → app_documents.id |
| user_id | INT | FK → users.id |
| user_name | NVARCHAR(255) | denormalized |
| assigned_by_id | INT | |
| assigned_by_name | NVARCHAR(255) | |
| assigned_at | DATETIME2 | |

**Constraint:** `uq_doc_user_assignment UNIQUE (doc_id, user_id)`

---

### `app_knowledge_store`
Key/value store with optional embeddings — used for persisting global knowledge facts and LLM-accessible lookup entries.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| key_name | NVARCHAR(255) UNIQUE | |
| value | NVARCHAR(MAX) | |
| embedding | NVARCHAR(MAX) | JSON float array (optional) |
| updated_at | DATETIME2 | |

---

### `app_schema_embedding_cache`
Caches embeddings for schema text so they don't need to be recomputed on every agent load.

| Column | Type | Notes |
|--------|------|-------|
| cache_key | CHAR(32) PK | MD5 hash of text |
| text_content | NVARCHAR(MAX) | |
| embedding | NVARCHAR(MAX) | JSON float array |
| created_at | DATETIME2 | |

---

### `app_search_cache`
Caches vector search results to reduce latency and embedding API calls.

| Column | Type | Notes |
|--------|------|-------|
| cache_key | CHAR(32) PK | MD5 hash of query+user |
| query | NVARCHAR(MAX) | |
| user_id | INT | |
| results | NVARCHAR(MAX) | JSON serialized results |
| hit_count | INT | how many times this was served |
| created_at | DATETIME2 | |
| expires_at | DATETIME2 | TTL-based expiry |

---

## 6. Document Watching & Connectors

Defined in: `database/app_db.py`, `services/document_watcher.py`, `services/sharepoint_watcher.py`

### `document_watch_dirs`
Local filesystem folders monitored for new/changed files.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| folder_path | NVARCHAR(1000) | absolute local path |
| label | NVARCHAR(255) | display name |
| scope | NVARCHAR(20) | `user`, `department`, `project`, `global` |
| scope_id | INT | |
| target_user_id | INT | which user's documents to assign ingested files to |
| created_by | INT | |
| created_at | DATETIME2 | |
| last_scanned | DATETIME2 | |
| enabled | BIT | |
| files_ingested | INT | running counter |
| key_metrics | NVARCHAR(MAX) | JSON stats |

**Constraint:** `uq_watch_dir UNIQUE (folder_path, target_user_id)`

---

### `sharepoint_watch_configs`
SharePoint sites/folders monitored via Microsoft Graph API polling.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| site_url | NVARCHAR(500) | SharePoint site URL |
| sp_folder_path | NVARCHAR(1000) | relative folder within the site |
| label | NVARCHAR(255) | |
| scope | NVARCHAR(20) | |
| scope_id | INT | |
| target_user_id | INT | |
| created_by | INT | |
| created_at | DATETIME2 | |
| last_scanned | DATETIME2 | |
| enabled | BIT | |
| files_ingested | INT | |
| poll_interval | INT | seconds between polls, default 60 |
| cached_site_id | NVARCHAR(500) | Graph API site id cache |
| cached_drive_id | NVARCHAR(500) | Graph API drive id cache |
| key_metrics | NVARCHAR(MAX) | JSON stats |

---

### `connector_logs`
Audit log for every file ingested (or failed) by a watcher.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| watch_type | NVARCHAR(20) | `folder` or `sharepoint` |
| watch_id | INT | FK → document_watch_dirs.id or sharepoint_watch_configs.id |
| watch_label | NVARCHAR(255) | denormalized |
| filename | NVARCHAR(500) | |
| status | NVARCHAR(20) | `success`, `error`, `skipped` |
| chunk_count | INT | |
| error_msg | NVARCHAR(MAX) | |
| created_at | DATETIME2 | |

---

## 7. NLQ & Schema Intelligence

Defined in: `database/app_db.py`, `nlq/nlq_engine.py`

### `app_nlq_learning`
Per-agent learned query examples used for few-shot NLQ prompting (good query/bad query pairs accumulated over time).

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| agent_name | NVARCHAR(255) UNIQUE | FK → app_agents.name |
| data | NVARCHAR(MAX) | JSON array of examples |
| updated_at | DATETIME2 | |

---

### `app_schema_graph`
NetworkX-style edge list representing table join relationships for a given agent. Used to compute optimal JOIN paths.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| agent_name | NVARCHAR(255) | FK → app_agents.name |
| table_a | NVARCHAR(255) | |
| table_b | NVARCHAR(255) | |
| join_key | NVARCHAR(255) | column used for the join |
| ref_key | NVARCHAR(255) | referenced column, default `id` |
| weight | FLOAT | graph traversal weight |
| schema_hash | NVARCHAR(64) | detects when schema changed |
| created_at | DATETIME2 | |

**Constraint:** `uq_schema_graph_edge UNIQUE (agent_name, table_a, table_b, join_key)`

---

## 8. Observability & Tracing

Defined in: `database/app_db.py`

### `app_retrieval_traces`
Logs every vector retrieval call — query, results, scores, latency — for RAG quality monitoring.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| session_id | NVARCHAR(100) | conversation session |
| user_id | INT | |
| query | NVARCHAR(MAX) | the retrieval query |
| doc_ids | NVARCHAR(MAX) | JSON array of returned doc ids |
| top_scores | NVARCHAR(MAX) | JSON array of cosine scores |
| method | NVARCHAR(50) | `hybrid`, `semantic`, `keyword` |
| result_count | INT | |
| confidence | FLOAT | |
| fallback | BIT | 1 if keyword fallback was used |
| latency_ms | INT | |
| created_at | DATETIME2 | |

---

## 9. Hub Agent System

Defined in: `sql/hub_tables.sql`, `sql/agents_hub_tables.sql`

### `hub_agents`
Agent definitions for the Hub (general-purpose LLM agents, as opposed to BI agents).

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| name | NVARCHAR(200) | |
| description | NVARCHAR(MAX) | |
| objective | NVARCHAR(MAX) | goal description shown to users |
| system_prompt | NVARCHAR(MAX) | LLM system prompt |
| model | NVARCHAR(100) | e.g. `gpt-4o`, `claude-sonnet-4-6` |
| temperature | FLOAT | default 0.7 |
| tools_json | NVARCHAR(MAX) | JSON array of tool ids |
| avatar_color | NVARCHAR(20) | hex color |
| status | NVARCHAR(20) | `active`, `inactive` |
| total_runs | INT | |
| total_tokens | INT | |
| department_id | INT | FK → departments.id |
| project_id | INT | FK → projects.id |
| created_by | INT | |
| created_at / updated_at | DATETIME2 | |

**Index:** `ix_hub_agents_status`

---

### `hub_conversations`
A chat session between a user and a hub agent.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| agent_id | NVARCHAR(36) | FK → hub_agents.id |
| user_id | INT | FK → users.id |
| title | NVARCHAR(500) | |
| created_at / updated_at | DATETIME2 | |

**Indexes:** `ix_hub_conversations_agent`, `ix_hub_conversations_user`

---

### `hub_messages`
Individual messages within a hub conversation.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| conversation_id | NVARCHAR(36) | FK → hub_conversations.id |
| role | NVARCHAR(20) | `user`, `assistant`, `tool` |
| content | NVARCHAR(MAX) | text content |
| tool_calls_json | NVARCHAR(MAX) | JSON array of tool call objects |
| tokens_used | INT | |
| created_at | DATETIME2 | |

**Index:** `ix_hub_messages_convo`

---

### `hub_tools`
Catalog of built-in tools available to hub agents.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(100) PK | slug e.g. `web_search` |
| name | NVARCHAR(100) UNIQUE | internal name |
| display_name | NVARCHAR(200) | |
| description | NVARCHAR(MAX) | |
| category | NVARCHAR(100) | grouping |
| schema_json | NVARCHAR(MAX) | JSON Schema for LLM function calling |
| enabled | BIT | |
| total_calls | INT | |
| success_calls | INT | |
| created_at | DATETIME2 | |

---

### `hub_knowledge_bases`
Documents/text files attached directly to hub agents as inline context.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| name | NVARCHAR(200) | |
| description | NVARCHAR(MAX) | |
| file_type | NVARCHAR(20) | `txt`, `pdf`, `md`, etc. |
| content | NVARCHAR(MAX) | raw text content |
| file_size | INT | bytes |
| chunk_count | INT | |
| created_at | DATETIME2 | |

---

### `hub_jobs`
Scheduled recurring runs for hub agents or workflows.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| name | NVARCHAR(200) | |
| description | NVARCHAR(MAX) | |
| job_type | NVARCHAR(50) | `agent` or `workflow` |
| target_id | NVARCHAR(36) | FK → hub_agents.id or hub_workflows.id |
| schedule | NVARCHAR(100) | cron expression |
| status | NVARCHAR(20) | `active`, `paused` |
| department_id | INT | |
| project_id | INT | |
| created_at | DATETIME2 | |

---

### `hub_agent_assignments`
Controls which users have access to which hub agents.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id CASCADE | |
| agent_id | NVARCHAR(36) | FK → hub_agents.id |
| agent_name | NVARCHAR(255) | denormalized |
| assigned_by | INT FK → users.id | |
| assigned_at | DATETIME2 | |

**Constraint:** `uq_haa_user_agent UNIQUE (user_id, agent_id)`  
**Indexes:** `IX_haa_user_id`, `IX_haa_agent_id`

---

## 10. Hub Workflows

Defined in: `sql/hub_tables.sql`

### `hub_workflows`
Visual workflow definitions (nodes + edges JSON graph). Supports sequential and parallel execution.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| name | NVARCHAR(200) | |
| description | NVARCHAR(MAX) | |
| nodes_json | NVARCHAR(MAX) | JSON array of node objects |
| edges_json | NVARCHAR(MAX) | JSON array of edge connections |
| execution_mode | NVARCHAR(20) | `sequential`, `parallel` |
| status | NVARCHAR(20) | `active`, `draft`, `archived` |
| total_runs | INT | |
| department_id | INT | |
| project_id | INT | |
| created_at / updated_at | DATETIME2 | |

---

### `hub_workflow_runs`
Execution record for each workflow run.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(36) PK | UUID |
| workflow_id | NVARCHAR(36) | FK → hub_workflows.id |
| user_id | INT | FK → users.id |
| status | NVARCHAR(20) | `running`, `completed`, `failed`, `waiting_approval` |
| input_data | NVARCHAR(MAX) | JSON initial input |
| output_data | NVARCHAR(MAX) | JSON final output |
| execution_log_json | NVARCHAR(MAX) | JSON array of step logs |
| started_at | DATETIME2 | |
| completed_at | DATETIME2 | |

**Indexes:** `ix_hub_wf_runs_workflow`, `ix_hub_wf_runs_user`

---

### `hub_workflow_assignments`
Controls which users have access to which workflows.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id CASCADE | |
| workflow_id | NVARCHAR(36) | FK → hub_workflows.id |
| workflow_name | NVARCHAR(255) | denormalized |
| assigned_by | INT FK → users.id | |
| assigned_at | DATETIME2 | |

**Constraint:** `uq_hwa_user_wf UNIQUE (user_id, workflow_id)`

---

## 11. Hub Approvals (HITL)

Defined in: `sql/hub_approvals.sql`

### `hub_approvals`
Human-in-the-loop approval requests. An agent or workflow node can pause and request a human decision before continuing.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| approval_id | NVARCHAR(36) UNIQUE | UUID, used by polling API |
| request_type | NVARCHAR(20) | `agent` or `workflow` |
| agent_id | NVARCHAR(36) | FK → hub_agents.id (nullable) |
| agent_name | NVARCHAR(200) | denormalized |
| workflow_id | NVARCHAR(36) | FK → hub_workflows.id (nullable) |
| workflow_name | NVARCHAR(200) | denormalized |
| conversation_id | NVARCHAR(36) | FK → hub_conversations.id |
| run_id | NVARCHAR(36) | FK → hub_workflow_runs.id |
| node_id | NVARCHAR(100) | specific workflow node that triggered this |
| requested_by_user_id | INT | who triggered the agent/workflow |
| assigned_to_user_id | INT | who needs to approve (nullable = any admin) |
| title | NVARCHAR(500) | human-readable description |
| context_json | NVARCHAR(MAX) | JSON — full context data for the approver |
| status | NVARCHAR(20) | `pending`, `approved`, `rejected` |
| approver_user_id | INT | who resolved it |
| approver_note | NVARCHAR(2000) | optional note |
| created_at | DATETIME2 | |
| resolved_at | DATETIME2 | |

**Indexes:** `ix_hub_approvals_status`, `ix_hub_approvals_assigned`, `ix_hub_approvals_requester`, `ix_hub_approvals_convo`, `ix_hub_approvals_run`

---

## 12. Hub Custom Tools

Defined in: `sql/hub_custom_tools.sql`

### `hub_custom_tools`
User-created Python function tools that hub agents can call. Code is stored and executed at runtime.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| name | NVARCHAR(100) UNIQUE | Python function name slug |
| display_name | NVARCHAR(200) | |
| description | NVARCHAR(MAX) | shown to LLM |
| category | NVARCHAR(100) | default `Custom` |
| pip_packages | NVARCHAR(MAX) | JSON array of required pip packages |
| imports_code | NVARCHAR(MAX) | Python import statements |
| function_code | NVARCHAR(MAX) | Python function body |
| input_schema | NVARCHAR(MAX) | JSON Schema for LLM function calling |
| output_desc | NVARCHAR(500) | describes return value |
| enabled | BIT | |
| total_calls | INT | |
| success_calls | INT | |
| created_by | INT | FK → users.id |
| created_at / updated_at | DATETIME2 | |

**Indexes:** `idx_custom_tools_name`, `idx_custom_tools_enabled`

---

## 13. Workspace (AI Workspace)

Defined in: `sql/workspace_tables.sql`

The Workspace module is an independent AI chat environment supporting multi-model conversations, artifacts, training data collection, and team collaboration.

### `ws_workspaces`
Top-level container for organizing conversations and projects.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| owner_id | INT FK → users.id | |
| name | NVARCHAR(255) | |
| description | NVARCHAR(1000) | |
| color | NVARCHAR(20) | |
| icon | NVARCHAR(50) | FontAwesome class |
| is_team | BIT | team vs personal workspace |
| is_archived | BIT | |
| created_at / updated_at | DATETIME2 | |

---

### `ws_workspace_members`
Team workspace membership.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| workspace_id | INT FK → ws_workspaces.id CASCADE | |
| user_id | INT FK → users.id | |
| role | NVARCHAR(20) | `owner`, `editor`, `member` |
| joined_at | DATETIME2 | |

**Constraint:** `UQ_ws_member UNIQUE (workspace_id, user_id)`

---

### `ws_projects`
Named project within a workspace with its own system prompt and model settings.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| workspace_id | INT FK → ws_workspaces.id | |
| owner_id | INT FK → users.id | |
| name | NVARCHAR(255) | |
| description | NVARCHAR(1000) | |
| system_prompt | NVARCHAR(MAX) | |
| model | NVARCHAR(100) | default model |
| temperature | FLOAT | |
| is_archived | BIT | |
| created_at / updated_at | DATETIME2 | |

---

### `ws_conversations`
A single conversation thread within the workspace.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| project_id | INT FK → ws_projects.id | nullable |
| workspace_id | INT FK → ws_workspaces.id | nullable |
| user_id | INT FK → users.id | |
| title | NVARCHAR(500) | |
| model | NVARCHAR(100) | model used |
| system_prompt | NVARCHAR(MAX) | conversation-level override |
| tools_enabled | NVARCHAR(500) | JSON array |
| is_starred | BIT | |
| is_archived | BIT | |
| message_count | INT | denormalized counter |
| total_tokens | INT | denormalized counter |
| created_at / updated_at | DATETIME2 | |

**Indexes:** `IX_ws_conversations_user (user_id, created_at DESC)`, `IX_ws_conversations_proj`

---

### `ws_messages`
Individual messages in a conversation.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| conversation_id | INT FK → ws_conversations.id CASCADE | |
| user_id | INT FK → users.id | |
| role | NVARCHAR(20) | `user`, `assistant`, `system`, `tool` |
| content | NVARCHAR(MAX) | |
| content_type | NVARCHAR(50) | `text`, `markdown`, `code` |
| model | NVARCHAR(100) | model that generated this |
| finish_reason | NVARCHAR(50) | OpenAI finish reason |
| prompt_tokens | INT | |
| completion_tokens | INT | |
| total_tokens | INT | |
| latency_ms | INT | |
| is_edited | BIT | |
| is_deleted | BIT | soft delete |
| parent_msg_id | INT | FK → ws_messages.id (threading) |
| sequence_num | INT | ordering within conversation |
| created_at | DATETIME2 | |

**Index:** `IX_ws_messages_conv (conversation_id, sequence_num)`

---

### `ws_message_chunks`
Stores streaming chunks of an assistant message for replay.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| message_id | INT FK → ws_messages.id CASCADE | |
| chunk_index | INT | |
| chunk_text | NVARCHAR(MAX) | |
| chunk_type | NVARCHAR(50) | `text`, `tool_call`, `tool_result` |
| created_at | DATETIME2 | |

**Index:** `IX_ws_chunks_msg (message_id, chunk_index)`

---

### `ws_tool_calls`
Records every tool call made during a message generation.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| message_id | INT FK → ws_messages.id CASCADE | |
| conversation_id | INT | denormalized |
| tool_call_id | NVARCHAR(100) | LLM-assigned id |
| tool_name | NVARCHAR(100) | |
| tool_input | NVARCHAR(MAX) | JSON |
| tool_output | NVARCHAR(MAX) | JSON |
| status | NVARCHAR(20) | `pending`, `success`, `error` |
| latency_ms | INT | |
| tokens_used | INT | |
| created_at | DATETIME2 | |

---

### `ws_citations`
Web citations returned with a message.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| message_id | INT FK → ws_messages.id CASCADE | |
| url | NVARCHAR(2000) | |
| title | NVARCHAR(1000) | |
| snippet | NVARCHAR(MAX) | |
| citation_index | INT | order in message |
| created_at | DATETIME2 | |

---

### `ws_artifacts`
Generated artifacts (code, files, outputs) associated with a message.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| conversation_id | INT FK → ws_conversations.id | |
| message_id | INT FK → ws_messages.id | |
| user_id | INT FK → users.id | |
| artifact_type | NVARCHAR(50) | `code`, `image`, `csv`, `pptx`, etc. |
| title | NVARCHAR(500) | |
| content | NVARCHAR(MAX) | inline content |
| language | NVARCHAR(50) | for code artifacts |
| file_path | NVARCHAR(1000) | for file artifacts |
| mime_type | NVARCHAR(100) | |
| byte_size | INT | |
| version | INT | |
| is_starred | BIT | |
| tags | NVARCHAR(500) | |
| created_at / updated_at | DATETIME2 | |

**Index:** `IX_ws_artifacts_user (user_id, artifact_type, created_at DESC)`

---

### `ws_files`
Uploaded files attached to conversations.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| conversation_id | INT FK → ws_conversations.id | |
| message_id | INT | FK → ws_messages.id |
| user_id | INT FK → users.id | |
| file_name | NVARCHAR(500) | |
| file_path | NVARCHAR(1000) | local path |
| mime_type | NVARCHAR(100) | |
| byte_size | INT | |
| openai_file_id | NVARCHAR(200) | if uploaded to OpenAI Files API |
| direction | NVARCHAR(10) | `upload` or `download` |
| created_at | DATETIME2 | |

---

### `ws_prompt_library`
Saved reusable prompt templates.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id | |
| workspace_id | INT FK → ws_workspaces.id | nullable (personal vs shared) |
| title | NVARCHAR(500) | |
| prompt_text | NVARCHAR(MAX) | |
| category | NVARCHAR(100) | |
| tags | NVARCHAR(500) | |
| use_count | INT | incremented on use |
| is_shared | BIT | |
| created_at / updated_at | DATETIME2 | |

**Index:** `IX_ws_prompt_user (user_id, category)`

---

### `ws_enterprise_memory`
Persistent key/value memory that survives across conversations.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id | |
| workspace_id | INT FK → ws_workspaces.id | nullable |
| memory_key | NVARCHAR(500) | |
| memory_value | NVARCHAR(MAX) | |
| source_conv_id | INT | which conversation created this |
| scope | NVARCHAR(20) | `user`, `workspace`, `global` |
| importance | INT | 1–10 priority score |
| is_active | BIT | |
| created_at / updated_at | DATETIME2 | |

**Index:** `IX_ws_memory_user (user_id, scope)`

---

### `ws_feedback`
Thumbs up/down feedback on individual messages.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| message_id | INT FK → ws_messages.id CASCADE | |
| user_id | INT FK → users.id | |
| rating | NVARCHAR(10) | `up`, `down` |
| comment | NVARCHAR(1000) | |
| created_at | DATETIME2 | |

---

### `ws_message_edits`
Audit trail of message content edits.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| message_id | INT FK → ws_messages.id CASCADE | |
| user_id | INT FK → users.id | |
| old_content | NVARCHAR(MAX) | |
| new_content | NVARCHAR(MAX) | |
| edited_at | DATETIME2 | |

---

### `ws_replay_events`
Event stream for conversation replay/audit.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| conversation_id | INT | |
| message_id | INT | nullable |
| event_type | NVARCHAR(100) | |
| event_data | NVARCHAR(MAX) | JSON |
| sequence_num | INT | |
| created_at | DATETIME2 | |

**Index:** `IX_ws_replay_conv (conversation_id, sequence_num)`

---

### `ws_activity_log`
Full audit trail of user actions in the workspace.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id | |
| action | NVARCHAR(100) | e.g. `create_conversation`, `export_artifact` |
| entity_type | NVARCHAR(50) | |
| entity_id | INT | |
| details | NVARCHAR(MAX) | JSON |
| ip_address | NVARCHAR(45) | |
| created_at | DATETIME2 | |

**Index:** `IX_ws_activity_user (user_id, created_at DESC)`

---

### `ws_model_usage`
Per-call LLM cost tracking for workspace conversations.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id | |
| model | NVARCHAR(100) | |
| conversation_id | INT | |
| prompt_tokens | INT | |
| completion_tokens | INT | |
| total_tokens | INT | |
| estimated_cost_usd | DECIMAL(10,6) | |
| call_type | NVARCHAR(50) | `chat`, `embed`, etc. |
| created_at | DATETIME2 | |

**Index:** `IX_ws_model_usage_user (user_id, created_at DESC)`

---

### `ws_user_settings`
Per-user key/value settings for the workspace UI.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| user_id | INT FK → users.id | |
| setting_key | NVARCHAR(200) | |
| setting_value | NVARCHAR(MAX) | |
| updated_at | DATETIME2 | |

**Constraint:** `UQ_ws_user_setting UNIQUE (user_id, setting_key)`

---

### `ws_copy_log` / `ws_retry_log`
Minor usage telemetry tables.

**`ws_copy_log`** — logs when a user copies message content.  
**`ws_retry_log`** — logs message retry attempts with error context.

---

## 14. Training Data

Defined in: `sql/training_tables.sql`, `sql/bi_training_tables.sql`

Training tables collect conversation data for fine-tuning LLMs.

### `ws_training_pairs`
Instruction/output pairs derived from workspace conversations.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| message_id | INT FK → ws_messages.id | source message |
| conversation_id | INT FK → ws_conversations.id | |
| workspace_id | INT FK → ws_workspaces.id | |
| instruction | NVARCHAR(MAX) | the prompt/question |
| context | NVARCHAR(MAX) | system prompt / context injected |
| output | NVARCHAR(MAX) | the assistant's response |
| quality_score | FLOAT | 0–100 auto-scored |
| quality_breakdown | NVARCHAR(MAX) | JSON score components |
| status | NVARCHAR(20) | `pending`, `auto_approved`, `approved`, `rejected`, `needs_review` |
| reviewed_by | INT FK → users.id | |
| reviewed_at | DATETIME | |
| review_notes | NVARCHAR(1000) | |
| domain | NVARCHAR(100) | topic domain tag |
| tags | NVARCHAR(500) | |
| language | NVARCHAR(10) | default `en` |
| model_used | NVARCHAR(100) | |
| token_count | INT | |
| has_citations | BIT | |
| has_artifact | BIT | |
| is_edited | BIT | was the message edited before saving |
| feedback_rating | NVARCHAR(20) | from ws_feedback |
| retry_count | INT | |
| created_at / updated_at | DATETIME | |

---

### `ws_training_preference_pairs`
DPO-style preference pairs (chosen vs rejected response) for RLHF training.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| base_pair_id | INT FK → ws_training_pairs.id | |
| conversation_id | INT FK → ws_conversations.id | |
| prompt | NVARCHAR(MAX) | |
| chosen_output | NVARCHAR(MAX) | preferred response |
| rejected_output | NVARCHAR(MAX) | dispreferred response |
| chosen_score | FLOAT | |
| rejected_score | FLOAT | |
| preference_source | NVARCHAR(50) | `human_feedback`, `edit_pair`, `retry_pair`, `annotation` |
| domain | NVARCHAR(100) | |
| tags | NVARCHAR(500) | |
| status | NVARCHAR(20) | `pending`, `approved`, `rejected` |
| reviewed_by | INT FK → users.id | |
| reviewed_at | DATETIME | |
| created_at | DATETIME | |

---

### `ws_training_exports`
Audit log of every training data export.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| exported_by | INT FK → users.id | |
| export_format | NVARCHAR(30) | `alpaca_jsonl`, `chatml_jsonl`, `openai_ft_jsonl`, `dpo_jsonl` |
| pair_count | INT | |
| preference_count | INT | |
| filters | NVARCHAR(MAX) | JSON — what filters were applied |
| file_name | NVARCHAR(500) | |
| file_size_bytes | INT | |
| content_hash | NVARCHAR(64) | |
| exported_at | DATETIME | |

---

### `ws_training_annotations`
Reviewer annotations on training pairs flagging errors or quality issues.

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| pair_id | INT FK → ws_training_pairs.id | |
| annotated_by | INT FK → users.id | |
| annotation_type | NVARCHAR(50) | `factual_error`, `hallucination`, `tone`, `length`, `format`, `other` |
| annotation_text | NVARCHAR(2000) | |
| severity | NVARCHAR(20) | `minor`, `major`, `critical` |
| created_at | DATETIME | |

---

### `bi_training_pairs`
Training pairs specifically for BI tool interactions (SQL queries, dashboards, reports).

| Column | Type | Notes |
|--------|------|-------|
| id | INT IDENTITY PK | |
| tool_type | NVARCHAR(50) | `bi_query`, `bi_dashboard`, `bi_report`, `bi_infographic`, `bi_presentation`, `bi_presentation_optimize` |
| user_id | INT FK → users.id | |
| agent_name | NVARCHAR(200) | FK → app_agents.name |
| instruction | NVARCHAR(MAX) | |
| context | NVARCHAR(MAX) | schema context used |
| output | NVARCHAR(MAX) | full LLM output |
| sql_query | NVARCHAR(MAX) | extracted SQL if applicable |
| quality_score | FLOAT | |
| quality_breakdown | NVARCHAR(MAX) | JSON |
| status | NVARCHAR(20) | `pending`, `auto_approved`, `approved`, `rejected`, `needs_review` |
| reviewed_by | INT FK → users.id | |
| reviewed_at | DATETIME | |
| review_notes | NVARCHAR(1000) | |
| model_used | NVARCHAR(100) | |
| token_count | INT | |
| feedback_rating | NVARCHAR(20) | |
| domain | NVARCHAR(100) | |
| tags | NVARCHAR(500) | |
| created_at / updated_at | DATETIME | |

---

## 15. Client Management

Defined in: `database/app_db.py`

### `app_clients`
Client records used by the PPT generator and email integration features.

| Column | Type | Notes |
|--------|------|-------|
| id | NVARCHAR(100) PK | |
| name | NVARCHAR(500) | client name |
| email_db_config | NVARCHAR(MAX) | JSON — email database connection config |
| all_our_services | NVARCHAR(MAX) | JSON array of service names |
| ppt_history | NVARCHAR(MAX) | JSON array of past presentations |
| extra_data | NVARCHAR(MAX) | JSON freeform |
| created_at / updated_at | DATETIME2 | |

---

## 16. Relationship Diagram (ERD Summary)

```
users ──────┬── user_sessions
            ├── user_departments ── departments
            ├── user_projects    ── projects
            ├── agent_assignments       (BI agent access)
            ├── hub_agent_assignments   (Hub agent access)
            ├── hub_workflow_assignments
            ├── agent_guardrails
            ├── dev_proxy_assignments
            ├── dev_resource_assignments
            ├── token_usage
            ├── app_document_assignments ── app_documents ─── app_document_chunks
            │                                              └── (parent_doc_id self-ref)
            ├── ws_workspaces ── ws_workspace_members
            │        └── ws_projects ── ws_conversations ── ws_messages ─┬── ws_message_chunks
            │                                                             ├── ws_tool_calls
            │                                                             ├── ws_citations
            │                                                             ├── ws_feedback
            │                                                             ├── ws_message_edits
            │                                                             └── ws_artifacts
            │                   ws_conversations ─────────────────────────── ws_files
            │                                                              └── ws_replay_events
            ├── ws_training_pairs ── ws_training_preference_pairs
            │        └── ws_training_annotations
            └── ws_training_exports

app_agents ─── app_jobs ── app_job_executions
           └── app_schema_graph
           └── app_nlq_learning
           └── (database_connection) ── app_database_connections

hub_agents ── hub_conversations ── hub_messages
hub_agents ── hub_approvals
hub_workflows ── hub_workflow_runs ── hub_approvals

document_watch_dirs     ──┬── app_documents
sharepoint_watch_configs ─┘       └── connector_logs
```

---

## Appendix: JSON Column Reference

Many columns store structured data as JSON strings (`NVARCHAR(MAX)`). Key ones:

| Table | Column | Shape |
|-------|--------|-------|
| `app_agents` | `selected_tables` | `["table1", "table2"]` |
| `app_agents` | `selected_columns` | `{"table1": ["col_a", "col_b"]}` |
| `app_jobs` | `schedule` | `{"type": "cron", "expression": "0 9 * * 1"}` |
| `app_jobs` | `delivery` | `{"type": "email", "to": ["..."]}` |
| `app_job_executions` | `artifacts` | `[{"type": "pptx", "path": "..."}]` |
| `hub_agents` | `tools_json` | `["web_search", "code_exec"]` |
| `hub_workflows` | `nodes_json` | `[{"id": "n1", "type": "agent", "agent_id": "..."}]` |
| `hub_workflows` | `edges_json` | `[{"from": "n1", "to": "n2"}]` |
| `hub_approvals` | `context_json` | `{"tool": "...", "input": {...}, "output_so_far": "..."}` |
| `hub_custom_tools` | `input_schema` | JSON Schema object |
| `ws_conversations` | `tools_enabled` | `["web_search", "code_exec"]` |
| `agent_guardrails` | `filter_rules` | `[{"column": "dept_id", "op": "=", "value": 3}]` |
| `app_clients` | `email_db_config` | `{"server": "...", "db": "...", "table": "..."}` |
| `ws_training_pairs` | `quality_breakdown` | `{"relevance": 80, "accuracy": 90, "format": 70}` |
