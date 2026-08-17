# Hub Agents — Deep Dive

A complete technical reference for how Hub Agents are defined, how the orchestrator drives them, how tools are registered and called, and how everything connects through the Flask blueprint.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Agent Definition (SQL Server)](#agent-definition)
3. [Orchestration Engine](#orchestration-engine)
4. [Short-Term Memory](#short-term-memory)
5. [Context Builder](#context-builder)
6. [Executor & Tool Calling Loop](#executor--tool-calling-loop)
7. [NDJSON Streaming Protocol](#ndjson-streaming-protocol)
8. [Tool Registry](#tool-registry)
9. [Built-in Tools Reference](#built-in-tools-reference)
10. [Custom Tools](#custom-tools)
11. [Hub Blueprint (HubExecutor & HubOrchestrator)](#hub-blueprint)
12. [Agent-to-Agent Communication](#agent-to-agent-communication)
13. [BI Agent Bridge](#bi-agent-bridge)
14. [Workflows](#workflows)
15. [Human-in-the-Loop Approvals](#human-in-the-loop-approvals)
16. [Token Accounting](#token-accounting)
17. [Access Control](#access-control)
18. [SQL Server Tables](#sql-server-tables)
19. [Key Libraries](#key-libraries)

---

## Architecture Overview

```
Browser / API Client
        │  HTTP POST /api/agenthub/agents/<id>/chat  (SSE / NDJSON)
        ▼
Flask Blueprint  (blueprints/agents_hub_bp.py)
        │
        ├─ HubOrchestrator  (wraps Orchestrator)
        │        │
        │        ├─ ContextBuilder  → builds system prompt + tool JSON schema
        │        │
        │        ├─ _call_llm()     → OpenAI /v1/chat/completions
        │        │        ↑ tool_choice="auto"
        │        │        │ response: tool_calls or final content
        │        │
        │        └─ HubExecutor.execute()  (one call per tool_call)
        │                 │
        │                 └─ TOOL_REGISTRY[tool_name]['function'](**params)
        │
        └─ HubWorkflowEngine  (for workflow runs)
                 │
                 └─ topological sort → node execution (agents, db, http, …)
```

---

## Agent Definition

Agents are rows in the `hub_agents` SQL Server table. Each agent carries:

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `name` | nvarchar | Display name |
| `description` | nvarchar | Short description shown to users |
| `objective` | nvarchar | Injected into the system prompt |
| `system_prompt` | nvarchar(max) | Free-form instructions for the LLM |
| `model` | nvarchar | `gpt-4o`, `gpt-4-turbo`, etc. |
| `temperature` | float | Sampling temperature |
| `tools_json` | nvarchar(max) | JSON array of enabled tool names or `{name, config}` objects |
| `env_vars_json` | nvarchar(max) | Per-agent env var overrides injected at execution time |
| `status` | nvarchar | `active` / `inactive` |
| `created_by` | int | FK → users table |

### Tool configuration in `tools_json`

```json
[
  "web_search",
  "query_database",
  {
    "name": "search_knowledge",
    "config": {
      "document_ids": "doc-uuid-1,doc-uuid-2"
    }
  },
  {
    "name": "communicate_with_agent",
    "config": {
      "agent_ids": "agent-uuid-a,agent-uuid-b"
    }
  }
]
```

Plain strings get no extra config; objects carry per-tool config that `HubExecutor` merges into every call.

---

## Orchestration Engine

**File:** `agents/core/orchestrator/engine.py`

```python
import json, time, urllib.request, urllib.error
from datetime import datetime, date
from decimal import Decimal
from typing import Generator
```

The core class is `Orchestrator`. It owns:

- `self.memory` — a `ShortTermMemory` instance (holds the last 10 messages)
- `self.executor` — an `Executor` instance (runs tools)
- `self.ctx` — a `ContextBuilder` instance (builds prompts)
- `self.max_loops = 10` — prevents runaway tool loops

### `_call_llm()` — raw OpenAI call

Uses `urllib` (no third-party SDK) to keep dependencies lean:

```python
def _call_llm(self, system: str, messages: list,
              tools: list = None, model: str = "gpt-4o") -> dict:
    payload = {
        "model":      model,
        "max_tokens": 4000,
        "messages":   [{"role": "system", "content": system}] + messages
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"   # let the model decide

    data = json.dumps(payload).encode('utf-8')
    req  = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=data,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}"
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())
```

---

## Short-Term Memory

**Class:** `ShortTermMemory` in `engine.py`

Keeps the last `max_messages` (default 10) turns. When the buffer fills:

```python
def _summarize_old(self):
    old          = self.messages[:-4]
    self.summary = f"[Earlier context: {len(old)} messages]"
    self.messages = self.messages[-4:]   # keep only the 4 most recent
```

The summary is injected as a `user` / `assistant` pair at the start of `get_history()` so the LLM always sees compressed prior context without losing the last 4 turns verbatim.

```python
def get_history(self) -> list:
    msgs = []
    if self.summary:
        msgs.append({"role": "user",      "content": f"[Memory]: {self.summary}"})
        msgs.append({"role": "assistant",  "content": "Understood."})
    for m in self.messages:
        msgs.append({"role": m["role"], "content": m["content"]})
    return msgs
```

---

## Context Builder

**Class:** `ContextBuilder` in `engine.py`

### System prompt

`build_system_prompt(agent, available_tools)` produces:

```
You are {agent.name}.
Objective: {agent.objective}

{agent.system_prompt}

Available Tools:
- web_search: Live web search using OpenAI's web_searc…
- query_database: Execute a SQL SELECT query against a …
…

Platform Document Capabilities:
…

Rules:
- Think step by step
- Use tools when needed, respond directly when sufficient
- Be concise and accurate
…
Current time: 2026-06-21 10:30 UTC
```

### Tool definitions

`build_tool_definitions(tool_names, registry)` converts the registry metadata into OpenAI function-calling format:

```python
{
  "type": "function",
  "function": {
    "name":        "query_database",
    "description": "Execute a SQL SELECT query … [Data Query]",
    "parameters": {
      "type":       "object",
      "properties": {
        "sql":             {"type": "string",  "description": "The SELECT statement to run"},
        "connection_name": {"type": "string",  "description": "Named connection to use"}
      },
      "required": ["sql"]
    }
  }
}
```

---

## Executor & Tool Calling Loop

### The agentic loop (`run()`)

```python
def run(self, user_input: str, agent: dict,
        conversation_history: list = None) -> Generator:
    # 1. Build tool list from agent.tools_json
    agent_tools = [t if isinstance(t, str) else t.get('name', '')
                   for t in agent.get('tools', [])]

    # 2. Build prompt & tool definitions
    system_prompt = self.ctx.build_system_prompt(agent, avail_tools)
    tool_defs     = self.ctx.build_tool_definitions(agent_tools, TOOL_REGISTRY)

    messages = [*last_6_history, {"role": "user", "content": user_input}]
    loop_count = 0

    while loop_count < self.max_loops:
        loop_count += 1

        # 3. Call LLM
        response = self._call_llm(system_prompt, messages, tool_defs, model)

        tool_calls  = response["choices"][0]["message"].get("tool_calls") or []
        content_txt = response["choices"][0]["message"].get("content") or ""

        # 4a. Tool execution branch
        if tool_calls:
            messages.append({"role": "assistant", "content": content_txt,
                             "tool_calls": tool_calls})
            tool_results = []
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])

                yield {"type": "tool_start", "tool": fn_name, "input": fn_args}

                result = self.executor.execute(fn_name, fn_args)  # ← runs the tool

                yield {"type": "tool_result", "tool": fn_name, "result": result}

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result)
                })

            messages.extend(tool_results)
            continue   # loop back → LLM sees tool results

        # 4b. Final answer branch (no tool_calls)
        if content_txt:
            yield {"type": "final", "content": content_txt,
                   "tokens_used": total_tokens, ...}
            return
```

### Base `Executor`

```python
class Executor:
    def execute(self, tool_name: str, params: dict) -> dict:
        from core.tools.registry import execute_tool
        start  = time.time()
        result = execute_tool(tool_name, {**params, "api_key": self.api_key})
        result['_execution_time'] = round(time.time() - start, 3)
        return result
```

`HubExecutor` (in `agents_hub_bp.py`) extends this and merges tool configs and hub context before the call.

---

## NDJSON Streaming Protocol

Every `yield` in `run()` is a JSON line. The frontend consumes these as Server-Sent Events:

| `type` | Payload | When |
|---|---|---|
| `status` | `{message, step}` | Start + each planning step |
| `tool_start` | `{tool, input, message}` | Before a tool executes |
| `tool_result` | `{tool, result, execution_time}` | After a tool returns |
| `final` | `{content, tokens_used, input_tokens, output_tokens, execution_log, loops}` | Agent produced its answer |
| `error` | `{message}` | Any failure |

---

## Tool Registry

**File:** `agents/core/tools/registry.py`

```python
import json, os, sys, time, tempfile
from datetime import datetime
```

### Registry structure

```python
TOOL_REGISTRY: dict[str, dict] = {
    "web_search": {
        "display_name": "Web Search",
        "category":     "Web",
        "description":  "Live web search using OpenAI's web_search_preview tool…",
        "function":     web_search,          # callable
        "schema": {
            "query": {
                "type":        "string",
                "required":    True,
                "description": "The search query"
            }
        },
        "icon": "search"
    },
    # … 30+ more tools
}
```

### `execute_tool()`

```python
def execute_tool(tool_name: str, params: dict) -> dict:
    if tool_name not in TOOL_REGISTRY:
        return {"error": f"Tool '{tool_name}' not found"}
    meta   = TOOL_REGISTRY[tool_name]
    schema = meta.get('schema', {})
    # Validate required params
    missing = [p for p, info in schema.items()
               if info.get('required') and p not in params]
    if missing:
        return {"error": f"Missing required params: {missing}"}
    return meta['function'](**params)
```

---

## Built-in Tools Reference

### Data & Database

| Tool | Library | What it does |
|---|---|---|
| `query_database` | `pyodbc`, `psycopg2`, `pymysql`, `sqlite3` | SELECT-only queries, supports MSSQL / PostgreSQL / MySQL / SQLite. Max 200 rows returned. |
| `update_company_watchlist` | `pyodbc` | Only write operation — INSERT/UPDATE `[News].[CompanyNames]`. Parameterized SQL, no string-built queries. |
| `analyze_csv_files` | `pandas` | Loads CSV/Excel from configured dirs. Two-phase: `preview_schema=True` → schema, then `code=<pandas code>` → execution in a sandboxed `exec()` scope. |

#### `query_database` — connection dispatch

```python
if db_type == 'mssql':
    for drv in ['ODBC Driver 18 for SQL Server',
                'ODBC Driver 17 for SQL Server', 'SQL Server']:
        try:
            cs   = f"DRIVER={{{drv}}};SERVER={server},{port or 1433};..."
            conn = pyodbc.connect(cs, timeout=15)
            break
        except Exception:
            continue

elif db_type == 'postgresql':
    import psycopg2
    conn = psycopg2.connect(host=..., port=..., ...)

elif db_type == 'mysql':
    import pymysql
    conn = pymysql.connect(host=..., port=..., ...)

elif db_type == 'sqlite':
    import sqlite3
    conn = sqlite3.connect(database, timeout=15)
```

### Knowledge & Documents

| Tool | What it does |
|---|---|
| `process_document` | Calls `agents.core.knowledge.document_processor.process_document()` — extracts text, chunks, embeds, stores in vector store. |
| `search_knowledge` | Calls `RAGPipeline.run()` — hybrid BM25 + vector search, cross-encoder reranking, confidence scoring, returns formatted context block. |
| `search_connector_knowledge` | Same as above but scoped to specific filesystem/SharePoint connector IDs. |
| `manage_knowledge` | CRUD on `app_knowledge_store` key-value table using SQL MERGE. |
| `search_documents` | Simpler keyword LIKE search against `hub_knowledge_bases` table (legacy). |

### Web & APIs

| Tool | Library | What it does |
|---|---|
| `web_search` | `urllib.request` → OpenAI Responses API (`gpt-4.1-mini` with `web_search_preview`), falls back to DuckDuckGo instant answers | Live web search. Primary uses OpenAI `/v1/responses`; fallback uses `api.duckduckgo.com` |
| `call_external_api` | `urllib.request` | Generic REST caller (GET/POST/PUT/DELETE). Auto-sets `Content-Type: application/json`. |

#### `web_search` flow

```python
# Primary: OpenAI Responses API
payload = {
    "model": "gpt-4.1-mini",
    "tools": [{"type": "web_search_preview"}],
    "input": f"Search the internet and answer…\n{query}"
}
# POST https://api.openai.com/v1/responses
# If output_text block found → return it

# Fallback: DuckDuckGo Instant Answer
params  = urllib.parse.urlencode({"q": query, "format": "json"})
# GET https://api.duckduckgo.com/?{params}
# Returns AbstractText / Answer / Definition or RelatedTopics
```

### Communication

| Tool | Library / Service | Env vars needed |
|---|---|---|
| `send_email` | `smtplib`, `email.mime` | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` |
| `send_sms` | Twilio REST API (`urllib`) | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` |
| `get_teams_chats_with_person` | Microsoft Graph API (`urllib`) | `TENANT_ID`, `BOT_APP_ID`, `BOT_APP_PASSWORD` |

#### Teams — auth flow

```python
# Client-credentials OAuth2 (app-only, requires Chat.Read.All admin consent)
data = urlencode({
    "grant_type":    "client_credentials",
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scope":         "https://graph.microsoft.com/.default",
})
# POST https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token
# → access_token

# Then: GET https://graph.microsoft.com/v1.0/users/{email}/chats?$expand=members
# → paginate up to 4 pages (200 chats), match by email or display name
# → GET /chats/{id}/messages?$top=50
# → strip HTML from message body, oldest-first output
```

### File Operations

| Tool | What it does |
|---|---|
| `create_text_file` | Writes to `Data/agent_store/files/` (or `AGENT_FILE_STORE` env override) |
| `load_text_file` | Reads from the file store; also searches extra `search_dirs` if configured |
| `folder_report` | Lists directory contents with sizes and modified dates; supports multi-path via `paths` config |
| `analyze_csv_files` | Loads CSV/Excel files from configured directories and runs pandas code |

### System & Code

| Tool | Library | What it does |
|---|---|
| `run_python_code` | `exec()` in sandboxed scope | Executes Python with whitelist imports only (`math`, `json`, `re`, `datetime`, `collections`, etc.). Blocked: `subprocess`, `socket`, `requests`, `open()`, `eval()`. |
| `check_system_status` | `psutil` (optional) | Returns CPU %, memory %, disk %, process uptime, active agent count |

#### Sandbox allowlist

```python
_ALLOWED_IMPORTS = {
    'math', 'json', 're', 'datetime', 'collections',
    'itertools', 'statistics', 'random', 'string',
    'decimal', 'fractions', 'functools',
}
```

---

## Custom Tools

**Stored in:** `hub_custom_tools` SQL Server table  
**Loaded by:** `register_custom_tool()` in `registry.py`

Custom tools let admins write Python functions through the UI. At registration:

1. The function code is read from the DB row.
2. A wrapper is inserted into `TOOL_REGISTRY` with the user-defined schema.
3. At execution time, `_exec_custom_tool()` in `agents_hub_bp.py` runs the code in a temporary venv with env vars injected.

```python
def register_custom_tool(tool_row: dict) -> None:
    name = tool_row['tool_key']
    TOOL_REGISTRY[name] = {
        "display_name": tool_row['display_name'],
        "category":     "Custom",
        "description":  tool_row['description'],
        "schema":       json.loads(tool_row.get('schema_json') or '{}'),
        "function":     lambda **kwargs: _delegate_to_hub_bp(name, kwargs),
        "icon":         "code"
    }
```

---

## Hub Blueprint

**File:** `blueprints/agents_hub_bp.py`

```python
import sys, os, uuid, json, logging, threading, re
from flask import Blueprint, render_template, jsonify, request, Response, stream_with_context
import auth, token_limits
from apscheduler.schedulers.background import BackgroundScheduler
```

### `HubExecutor`

Extends `Orchestrator.Executor` to:

- Inject `_hub_ctx` (user session, agent ID, conversation ID) into every tool call.
- Merge per-tool `config` from `tools_json` (e.g. document_ids, agent_ids) with runtime params.
- Forward `api_key` from hub config (falls back to `OPENAI_API_KEY` env var).

```python
class HubExecutor(Executor):
    def __init__(self, api_key: str, hub_ctx: dict, tool_configs: dict):
        super().__init__(api_key)
        self.hub_ctx      = hub_ctx
        self.tool_configs = tool_configs  # {tool_name: {config_key: value}}

    def execute(self, tool_name: str, params: dict) -> dict:
        # Merge tool-specific config from agent definition
        extra = self.tool_configs.get(tool_name, {})
        merged = {**extra, **params,
                  "_hub_ctx": self.hub_ctx,
                  "api_key":  self.api_key}
        start  = time.time()
        result = execute_tool(tool_name, merged)
        result['_execution_time'] = round(time.time() - start, 3)
        return result
```

### `HubOrchestrator`

Wraps `Orchestrator` and enriches the system prompt with allowed agent names so the LLM cannot invent agent identifiers:

```python
class HubOrchestrator(Orchestrator):
    def __init__(self, api_key: str, hub_ctx: dict):
        super().__init__(api_key)
        self.hub_ctx = hub_ctx

    def run(self, user_input: str, agent: dict, history: list):
        # Replace executor with hub-aware version
        tool_configs = self._extract_tool_configs(agent.get('tools', []))
        self.executor = HubExecutor(self.api_key, self.hub_ctx, tool_configs)

        # Enrich system prompt with allowed agent / BI agent names
        agent = self._enrich_agent_def(agent)
        yield from super().run(user_input, agent, history)

    def _enrich_agent_def(self, agent: dict) -> dict:
        # Find communicate_with_agent config → inject allowed_agents names into system prompt
        # Find communicate_with_data_agent config → inject allowed_bi_agents names
        # Prevents LLM from hallucinating agent names
        ...
```

---

## Agent-to-Agent Communication

**Tool:** `communicate_with_agent`

```python
def communicate_with_agent(agent_id: str, message: str, **kwargs) -> dict:
    # 1. Lookup target agent by UUID or name
    rows = _hub_db_query(
        "SELECT * FROM hub_agents WHERE id=? OR name=?",
        (agent_id, agent_id), fetchall=True
    )

    # 2. Enforce allowed_ids whitelist (from tool config in calling agent)
    allowed_ids = kwargs.get('agent_ids', [])  # injected by HubExecutor

    # 3. Spin up a sub-orchestrator for the target agent
    hub_bp = sys.modules.get('agents_hub_bp')
    orch   = hub_bp.HubOrchestrator(api_key, sub_ctx)
    chunks = list(orch.run(message, agent_row, []))

    # 4. Extract final answer + bubble up token usage
    for chunk in chunks:
        data = json.loads(chunk)
        if data.get('type') == 'final':
            final_answer = data['content']
            _sub_in_tok  = data['input_tokens']
            _sub_out_tok = data['output_tokens']

    return {
        "success":       True,
        "response":      final_answer,
        "_tokens_used":  _sub_tot_tok,     # consumed by parent orchestrator
        "_input_tokens": _sub_in_tok,
        "_output_tokens":_sub_out_tok,
    }
```

The parent orchestrator strips the private `_tokens_*` fields before passing the result to the LLM, and adds them to its own running token totals.

---

## BI Agent Bridge

**Tool:** `communicate_with_data_agent`

```python
def communicate_with_data_agent(agent_name: str, question: str, **kwargs) -> dict:
    # 1. Resolve from running app module
    app_mod           = sys.modules.get("app")
    agent_manager_obj = getattr(app_mod, "agent_manager")
    nlq_engine_obj    = getattr(app_mod, "nlq_engine")

    # 2. Access control (non-admin users restricted to assigned BI agents)
    if user_role not in ("admin", "dev"):
        user_assigned = auth.get_assigned_agents(user_id)
        # intersect with allowed_bi from tool config

    # 3. Apply per-user guardrail (mandatory WHERE clauses, table restrictions)
    _guardrail = org_db.get_agent_guardrail(user_id, agent_name, "bi")
    question   = "[MANDATORY FILTER: …]\n\nUser question: " + question

    # 4. Token accumulator callback
    def _da_token_recorder(call_type, tokens, input_tokens=0, output_tokens=0):
        _da_tok_acc["total"] += tokens
        ...

    # 5. Run NLQ engine
    result = nlq_engine_obj.process_question(
        question=question,
        agent_config=agent_config,
        connection_name=agent_config["database_connection"],
        session_id=session_id,
        user_context=user_ctx,
        extra_token_recorder=_da_token_recorder,
    )

    return {
        "success":        True,
        "answer":         result.get("answer"),
        "data":           result.get("data"),
        "sql":            result.get("sql_query"),
        "_tokens_used":   _da_tok_acc["total"],
        "_input_tokens":  _da_tok_acc["input"],
        "_output_tokens": _da_tok_acc["output"],
    }
```

---

## Workflows

**Class:** `HubWorkflowEngine` in `agents_hub_bp.py`

Workflows are JSON documents stored in `hub_workflows`. The engine executes them as DAGs.

### Workflow JSON schema

```json
{
  "name": "Monthly Report",
  "execution_mode": "sequential",
  "nodes": [
    {"id": "n1", "type": "query_db",  "connection_name": "prod", "query": "SELECT …"},
    {"id": "n2", "type": "agent",     "agent_id": "uuid",        "question": "Summarise: {{n1.data}}"},
    {"id": "n3", "type": "email_send","to": "boss@co.com",       "body": "{{n2.response}}"},
    {"id": "n4", "type": "condition", "expression": "{{n1.row_count}} > 0",
                 "out_true": "n2", "out_false": "n_skip"}
  ],
  "edges": [
    {"from": "n1", "to": "n2"},
    {"from": "n2", "to": "n3"}
  ]
}
```

### Node types

| Type | What it does |
|---|---|
| `agent` | Runs a hub agent with `question`. Stores output as `{{nodeId.response}}`. |
| `query_db` | Executes a SQL SELECT. Stores rows as `{{nodeId.data}}`. |
| `db_write` | Executes a write SQL statement. |
| `file_read` / `file_write` | Read/write files from the agent store. |
| `http_request` | Makes an HTTP call; stores JSON response. |
| `email_send` / `email_read` | SMTP send / IMAP read. |
| `loop_start` / `loop_end` | Iterates over an array variable. |
| `condition` | Evaluates an expression; routes to `out_true` or `out_false` edge. |
| `transform` | Python expression to reshape variables. |
| `set_variable` | Sets a named variable for downstream nodes. |
| `delay` | Waits N seconds. |
| `output` | Marks the workflow's final output node. |

### Execution

```python
# 1. Topological sort (Kahn's algorithm)
order = kahn_sort(nodes, edges)

# 2. Execute each node in order
for node_id in order:
    node   = nodes[node_id]
    result = execute_node(node, variable_store)
    variable_store[node_id] = result

    # Variable substitution: {{nodeId.field}} → resolved value
    # Condition branches: check expression, follow correct edge
    # Loop: repeat body nodes for each item in array
```

### Approval checkpoint

When `APPROVAL=true` is set and an agent node calls `request_human_approval`:

```python
_ss_exec("""
    INSERT INTO hub_approvals
        (approval_id, request_type, agent_id, title, context_json)
    VALUES (?, 'agent', ?, ?, ?)
""", approval_id, agent_id, title, ctx_blob)
```

Workflow execution pauses. An admin approves/rejects via the Approvals page, which calls a resume endpoint to continue from the paused node.

---

## Human-in-the-Loop Approvals

Enabled by `APPROVAL=true` env var. Registered as a real tool in `TOOL_REGISTRY`:

```python
def _request_human_approval_fn(title='', context='',
                                assigned_to_user_id=None, **kwargs) -> dict:
    hub_ctx     = kwargs.get('_hub_ctx') or {}
    approval_id = str(uuid.uuid4())

    _ss_exec("""
        INSERT INTO hub_approvals
            (approval_id, request_type, agent_id, agent_name,
             conversation_id, requested_by_user_id, assigned_to_user_id,
             title, context_json)
        VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, ?)
    """, approval_id, ...)

    return {
        "success":     True,
        "approval_id": approval_id,
        "status":      "pending",
        "message":     "Approval request created. Waiting for human review.",
    }
```

The agent is then expected to poll or the human's approval triggers a webhook that resumes the conversation.

---

## Token Accounting

Tokens are tracked across nested calls (sub-agents, BI agents) via private fields on tool results:

```python
# In Orchestrator.run() — after every tool execution:
_t_in  = int(result.pop('_input_tokens',  0) or 0)
_t_out = int(result.pop('_output_tokens', 0) or 0)
_t_tot = int(result.pop('_tokens_used',   0) or 0) or (_t_in + _t_out)
input_tokens  += _t_in
output_tokens += _t_out
total_tokens  += _t_tot
```

These private `_` fields are stripped before the result reaches the LLM's context window. The final `type=final` NDJSON chunk includes the true combined total across all loops and nested calls.

`token_limits` module enforces per-user or per-agent monthly caps checked before each run.

---

## Access Control

| Role | Access |
|---|---|
| `user` | Only agents/workflows assigned to them via `hub_agent_assignments` / `hub_workflow_assignments` |
| `dev` | All agents, all workflows, all API endpoints |
| `admin` | Everything dev has + assignment management (`hub_agent_assignments`) |

Assignment check (example):

```python
def get_assigned_agent_ids(user_id: int) -> list:
    rows = _ss_exec(
        "SELECT agent_id FROM hub_agent_assignments WHERE user_id = ?",
        (user_id,), fetchall=True
    )
    return [r['agent_id'] for r in rows]
```

---

## SQL Server Tables

| Table | Purpose |
|---|---|
| `hub_agents` | Agent definitions (tools, prompts, model) |
| `hub_agent_assignments` | user_id → agent_id (access control) |
| `hub_workflows` | Workflow DAG JSON |
| `hub_workflow_assignments` | user_id → workflow_id |
| `hub_approvals` | Pending/approved/rejected HITL approval requests |
| `hub_tools` | Tool metadata and call statistics (success/total counts) |
| `hub_conversations` | Saved conversation history |
| `hub_custom_tools` | User-defined tool code + schema |
| `hub_knowledge_bases` | Legacy per-hub knowledge documents |
| `app_knowledge_store` | Key-value persistent knowledge (manage_knowledge tool) |

---

## Key Libraries

| Library | Version requirement | Usage |
|---|---|---|
| `flask` | Any | Blueprint, routing, SSE streaming |
| `pyodbc` | Any | SQL Server (MSSQL) connections |
| `psycopg2` | Any | PostgreSQL connections |
| `pymysql` | Any | MySQL connections |
| `pandas` | Any | CSV/Excel analysis in `analyze_csv_files` |
| `apscheduler` | `>=3.0` | Scheduled tool jobs (cron-based, daemon background thread) |
| `psutil` | Optional | `check_system_status` CPU/memory/disk metrics |
| `sentence_transformers` | `>=2.0` | Embedding model for `search_knowledge` (via RAG pipeline) |
| `urllib` (stdlib) | — | All HTTP calls (OpenAI, DuckDuckGo, Teams, Twilio, external APIs) |
| `smtplib` (stdlib) | — | `send_email` |
| No OpenAI SDK | — | Direct `urllib` POST to `api.openai.com/v1/chat/completions` |
