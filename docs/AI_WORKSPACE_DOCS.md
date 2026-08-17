# Enterprise AI Workspace — Complete Documentation

**Platform:** Nexus  
**Module:** Enterprise AI Workspace  
**Added to:** Existing Flask + SQL Server platform  
**AI Provider:** OpenAI API only (no local models)

---

## What Is the AI Workspace?

The AI Workspace is a **fully internal ChatGPT/Claude-style AI assistant** embedded inside your existing Nexus portal. It is NOT a chatbot widget — it is a complete AI productivity platform where every conversation, every token, every tool call, and every generated artifact is permanently recorded in your own SQL Server database.

It was designed for three purposes:

1. **Daily enterprise use** — staff can chat with powerful AI models, run web searches, and generate code/SQL/reports inside the company portal
2. **Full intelligence capture** — everything the AI produces is stored permanently for future reference, compliance, and analytics
3. **Replay-ready architecture** — every streaming chunk is stored so any conversation can be replayed event-by-event exactly as it happened

---

## Why Was the Response Slow?

### The Root Cause

Your SQL Server is at `192.168.51.10` (a network machine, not localhost). The original streaming code was doing **2 SQL network round-trips per streaming chunk**:

```
OpenAI sends chunk "Hello" →
  → save_chunk() writes to 192.168.51.10  (5–15 ms round-trip)
  → log_replay_event() writes to 192.168.51.10  (5–15 ms round-trip)
  → then sends "Hello" to your browser
```

For a typical GPT-4o response of ~300 streaming chunks, that is **600 SQL queries** between each word appearing on screen. Even at 5ms each, that adds **3 seconds of pure DB overhead** blocking the stream.

### The Fix (Implemented)

All DB writes during streaming are now **buffered in memory**. The browser receives chunks instantly. After the stream finishes, a background thread flushes everything to the DB in one batch.

```
Before fix (per chunk):       After fix (entire response):
  chunk → DB write × 2          ALL chunks → buffer (RAM)
  chunk → DB write × 2          stream done → 1 batch INSERT
  chunk → DB write × 2          everything else → background thread
  ...
  300 chunks = 600 SQL queries  1 batch INSERT + ~8 queries total
```

The user sees the response stream immediately. The DB is written to asynchronously.

### Other Slowness Factors

| Factor | Impact | Notes |
|---|---|---|
| OpenAI API latency | **High** | First-token delay 0.5–3s is normal — OpenAI servers are overseas |
| `gpt-4o` model | Medium | Slower than `gpt-4o-mini`. Switch models for faster responses |
| Flask dev server (`debug=True`) | Low | Single-threaded but fine for internal use |
| Auto-title generation | Was slow | Now runs in the background thread — zero user-facing delay |
| Network to SQL Server | Was critical | Now irrelevant during streaming (buffered) |
| Per-request pyodbc connections | Low | Each query opens a new connection — acceptable for this scale |

### How to Make It Even Faster

1. **Use `gpt-4o-mini`** as your default model in Workspace Settings — it is 3–5× faster to start streaming and 95% cheaper
2. **Disable "Save streaming chunks"** in Workspace Settings if you don't need per-chunk replay (fewer DB writes)
3. Use a **production WSGI server** instead of Flask dev: `pip install waitress` then `waitress-serve --port=5000 app:app`

---

## Files Created

### Backend

#### `workspace_tables.sql`
SQL Server table creation script. Run this **once** in SSMS against your `nexus` database before using the workspace.

Contains 19 tables:

| Table | Purpose |
|---|---|
| `ws_workspaces` | Top-level containers (personal or team) |
| `ws_workspace_members` | Team workspace membership |
| `ws_projects` | Projects within workspaces (persistent system prompts) |
| `ws_conversations` | Chat threads |
| `ws_messages` | Individual messages (user + assistant) |
| `ws_message_chunks` | Streaming chunks stored for replay |
| `ws_tool_calls` | Tool call records (web search, etc.) |
| `ws_citations` | Web search citation URLs and snippets |
| `ws_artifacts` | Generated code, SQL, HTML, reports, etc. |
| `ws_files` | Uploaded/downloaded files |
| `ws_prompt_library` | Saved/shared reusable prompts |
| `ws_replay_events` | Full event stream for conversation replay |
| `ws_enterprise_memory` | Persistent facts the AI should know |
| `ws_feedback` | Thumbs up/down ratings on messages |
| `ws_message_edits` | History of message edits |
| `ws_copy_log` | Tracks when users copy message content |
| `ws_retry_log` | Tracks retried requests |
| `ws_activity_log` | Full workspace activity audit trail |
| `ws_model_usage` | Per-model token and cost tracking |
| `ws_user_settings` | Per-user workspace preferences |

---

#### `workspace_db.py`
Database layer — all CRUD operations for every workspace table.

Follows the exact same pattern as your existing `auth.py`:
- Uses `pyodbc` with `config.DB_CONFIG`
- Returns `list[dict]` from all queries
- Serialises datetime objects to ISO strings automatically

Key functions:

```python
# Conversations
create_conversation(user_id, model, system_prompt, ...)  → int (id)
get_conversations(user_id, ...)                          → list[dict]
get_conversation(conversation_id, user_id)               → dict
update_conversation(conversation_id, data)               → bool
delete_conversation(conversation_id)                     → bool   # soft delete

# Messages
save_message(conversation_id, user_id, role, content, ...)  → int (id)
update_message(message_id, content, tokens, ...)            → None
get_conversation_messages(conversation_id, limit=100)        → list[dict]

# Chunks (for replay)
batch_save_chunks(message_id, [(index, text), ...])      → None  # fast batch insert

# Citations
save_citation(message_id, citation_dict, index)          → None
get_message_citations(message_id)                        → list[dict]

# Artifacts
save_artifact(user_id, type, title, content, ...)        → int (id)
get_artifacts(user_id, artifact_type, limit)             → list[dict]

# Prompt library
create_prompt(user_id, title, prompt_text, ...)          → int (id)
get_prompts(user_id, include_shared, category)           → list[dict]

# Enterprise memory
save_memory_entry(user_id, key, value, scope, ...)       → int (id)
get_memory(user_id, scope)                               → list[dict]

# Replay
log_replay_event(conv_id, msg_id, event_type, data)      → None
get_replay_events(conversation_id)                       → list[dict]

# Analytics
get_workspace_analytics(user_id, days)                   → dict
get_model_usage_summary(user_id, days)                   → list[dict]
get_daily_model_usage(user_id, days)                     → list[dict]
```

---

#### `workspace_openai_service.py`
OpenAI API wrapper. Handles streaming with **automatic fallback**.

**Web search enabled** → uses OpenAI Responses API (`web_search_preview` tool) which returns citations automatically.  
**Web search disabled** → uses Chat Completions API (more stable, always available).  
**Responses API fails** → automatically falls back to Chat Completions.

```python
# Main entry point — yields event dicts
stream_workspace_chat(
    messages=[{"role": "user", "content": "..."}],
    model="gpt-4o",
    tools_enabled=["web_search"],  # or []
    system_prompt="You are...",
    temperature=0.7,
)

# Event types yielded:
# {"type": "chunk",      "text": "Hello"}
# {"type": "tool_start", "tool": "web_search", "query": "latest AI news"}
# {"type": "tool_done",  "tool": "web_search"}
# {"type": "done",       "full_text": "...", "prompt_tokens": 150,
#                        "completion_tokens": 300, "total_tokens": 450,
#                        "latency_ms": 2300, "estimated_cost": 0.00415,
#                        "citations": [{"url": "...", "title": "..."}]}
# {"type": "error",      "message": "..."}

# Auto-title (uses gpt-4o-mini, cheap + fast)
generate_conversation_title(first_message)  → str
```

**Cost table** (USD per 1K tokens, built-in):

| Model | Prompt | Completion |
|---|---|---|
| gpt-4o | $0.0025 | $0.010 |
| gpt-4o-mini | $0.00015 | $0.0006 |
| gpt-4-turbo | $0.010 | $0.030 |
| o1 | $0.015 | $0.060 |
| o1-mini | $0.003 | $0.012 |

---

#### `workspace_bp.py`
Flask Blueprint — registered as `workspace` with no prefix.

**Page routes** (all `@auth.login_required`):

| URL | Function | Template |
|---|---|---|
| `/workspace` | `workspace_chat` | `workspace/chat.html` |
| `/workspace/projects` | `workspace_projects` | `workspace/projects.html` |
| `/workspace/saved-chats` | `workspace_saved_chats` | `workspace/saved_chats.html` |
| `/workspace/prompt-library` | `workspace_prompt_library` | `workspace/prompt_library.html` |
| `/workspace/artifacts` | `workspace_artifacts` | `workspace/artifacts.html` |
| `/workspace/team` | `workspace_team` | `workspace/team.html` |
| `/workspace/memory` | `workspace_memory` | `workspace/memory.html` |
| `/workspace/activity` | `workspace_activity` | `workspace/activity.html` |
| `/workspace/analytics` | `workspace_analytics` | `workspace/analytics.html` |
| `/workspace/replay` | `workspace_replay` | `workspace/replay.html` |
| `/workspace/model-usage` | `workspace_model_usage` | `workspace/model_usage.html` |
| `/workspace/settings` | `workspace_settings` | `workspace/settings.html` |

**API routes** (all JSON, all `@auth.login_required`):

| Method | URL | Purpose |
|---|---|---|
| `POST` | `/api/workspace/chat/stream` | **SSE streaming chat** (main endpoint) |
| `GET/POST` | `/api/workspace/conversations` | List / create conversations |
| `GET/PATCH/DELETE` | `/api/workspace/conversations/<id>` | Get / update / delete |
| `GET/POST` | `/api/workspace/projects` | List / create projects |
| `PUT/DELETE` | `/api/workspace/projects/<id>` | Update / delete |
| `GET/POST/DELETE` | `/api/workspace/artifacts` | Artifacts CRUD |
| `POST` | `/api/workspace/artifacts/<id>/star` | Toggle star |
| `GET/POST/DELETE` | `/api/workspace/prompts` | Prompt library CRUD |
| `POST` | `/api/workspace/prompts/<id>/use` | Increment use count |
| `GET/POST/DELETE` | `/api/workspace/memory` | Enterprise memory CRUD |
| `POST` | `/api/workspace/messages/<id>/feedback` | thumbs_up / thumbs_down |
| `POST` | `/api/workspace/messages/<id>/copy` | Log copy event |
| `PATCH` | `/api/workspace/messages/<id>/edit` | Edit message + save history |
| `GET` | `/api/workspace/replay/<conv_id>` | Full replay event stream |
| `GET` | `/api/workspace/analytics/summary` | Analytics summary |
| `GET` | `/api/workspace/model-usage` | Model usage stats |
| `GET/POST` | `/api/workspace/settings` | User settings |
| `GET` | `/api/workspace/activity` | Activity log |

---

### Frontend

#### `templates/workspace/chat.html`
The main AI Workspace page. Full ChatGPT-style layout:
- Left panel: conversation list with project filter
- Top bar: model selector, web search toggle, star/delete buttons
- Message area: streaming bubbles with markdown rendering
- Input area: auto-resizing textarea, file attach, send button

Uses [workspace_chat.js](static/js/workspace_chat.js) for all logic.

#### Other Templates (extend `base.html`, all with sidebar)

| Template | What it shows |
|---|---|
| `projects.html` | Project cards — create/edit/delete. Each project has a name, description, system prompt, and default model |
| `saved_chats.html` | All conversations — starred section + recent section, with search filter |
| `prompt_library.html` | Cards for all saved prompts — create, search by category, use (opens workspace with pre-filled text) |
| `artifacts.html` | Cards for all generated artifacts — filter by type, view in modal, star, delete |
| `team.html` | Team workspace cards — create shared workspaces for groups |
| `memory.html` | Enterprise memory entries — importance dots (1–10), scope (user/workspace/enterprise) |
| `activity.html` | Full activity log table — timestamped, action badges, searchable |
| `analytics.html` | KPI cards + Chart.js daily usage chart + top models list |
| `replay.html` | Two-column: select conversation → press Replay → watch stream event-by-event with speed control |
| `model_usage.html` | Per-model token/cost breakdown + Chart.js bar chart + admin all-users table |
| `settings.html` | Default model, system prompt, temperature, font size, toggle preferences |

---

#### `static/css/workspace.css`
Scoped workspace styles. Uses your existing CSS variables (`--primary-color`, `--sidebar-bg`, etc.).  
Key sections: two-column layout, message bubbles, streaming cursor, code blocks, typing indicator, tool indicators, citation chips, artifact/project/memory/KPI cards, empty states.

#### `static/js/workspace_chat.js`
Complete frontend logic module (`WS` namespace):

| Function | What it does |
|---|---|
| `WS.init()` | Boot — applies settings, loads URL-param conversation, handles `?prompt=` pre-fill |
| `WS.sendMessage()` | Reads input, hits SSE endpoint, drives streaming display |
| `WS.loadConversation(id)` | Fetches all messages + citations from DB, re-renders chat |
| `WS.newConversation()` | Clears UI, resets state, shows welcome screen |
| `WS.openArtifact(code, lang)` | Opens artifact viewer modal (for code/SQL blocks) |
| `WS.saveArtifact()` | POSTs current artifact to `/api/workspace/artifacts` |
| `formatContent(text)` | Markdown renderer: bold, italic, code blocks with copy button, headings, lists |
| `finaliseBubble()` | After stream ends: re-renders bubble with full markdown, adds action buttons |
| `buildMsgActions()` | Copy button + thumbs up/down feedback buttons |

---

### Modified Existing Files

#### `templates/components/sidebar.html`
Added **AI Workspace** submenu with 12 navigation items between the Jobs section and User Management. Uses `request.endpoint.startswith('workspace.')` for active-state detection.

#### `app.py`
Two lines added:
```python
from workspace_bp import workspace_bp       # import
app.register_blueprint(workspace_bp)        # register
```

#### `token_limits.py`
Added `workspace_chat` to `CALL_TYPES` so AI Workspace usage appears in your existing daily token budget tracking.

---

## How It All Works Together — Data Flow

### Sending a message

```
1. User types in chat.html and presses Enter
2. workspace_chat.js → POST /api/workspace/chat/stream
3. workspace_bp.py:
   a. Token limit check (existing system)
   b. Create conversation if first message
   c. Fetch message history from DB
   d. Save user message to DB
   e. Create placeholder assistant message in DB
   f. Send SSE "start" event to browser
4. workspace_openai_service.py:
   a. If web_search enabled → Responses API with web_search_preview
   b. Otherwise → Chat Completions streaming
   c. Yields events: chunk, tool_start, tool_done, done, error
5. workspace_bp.py streams each "chunk" event directly to browser
   (NO DB writes during streaming — all buffered in RAM)
6. Browser receives chunks → appendChunkToBubble() → text appears
7. When "done" event arrives:
   a. Browser: finalises bubble with full markdown rendering
   b. Background thread starts: batch writes all chunks, citations,
      token usage, model usage, conversation stats, auto-title
8. User sees complete response; DB writes finish ~1 second later
```

### Replay of a conversation

```
1. User goes to /workspace/replay
2. Clicks a conversation → GET /api/workspace/replay/<id>
3. Returns all ws_replay_events in sequence order
4. Client-side player reads events one by one:
   user_message → renders user bubble
   chunk        → appends text to assistant bubble character by character
   tool_start   → shows "Searching…" indicator
   tool_done    → shows check mark
   message_done → shows token/latency meta
   error        → shows error alert
5. Speed controlled by slider (Fast/Normal/Slow)
```

### What is stored permanently

Every single one of these is recorded forever in your SQL Server:

- Every conversation (title, model, system prompt, timestamps)
- Every user message and assistant response (full text, token counts, latency)
- Every streaming chunk (for exact replay)
- Every tool call (web search queries and results)
- Every citation (URL, title, snippet)
- Every generated artifact (code, SQL, HTML, reports)
- Every prompt library entry
- Every enterprise memory entry
- Every thumbs up/down feedback
- Every message edit with old and new content
- Every copy event
- Every retry
- Every workspace action in the activity log
- Per-model token and USD cost tracking
- Per-user workspace settings

---

## Setup Checklist

### Step 1: Run SQL tables (once)
Open SSMS, connect to `nexus`, open `workspace_tables.sql`, run it.

### Step 2: Restart Flask
```
python app.py
```
The AI Workspace section appears in the sidebar immediately.

### Step 3: Start chatting
Go to `/workspace`. Select a model. Type a message. The rest is automatic.

### Step 4: Enable web search (optional)
Toggle the **Search** button in the chat top bar. This uses OpenAI's `web_search_preview` tool. Requires `openai>=1.50.0`.

Check your version:
```bash
pip show openai
```
Update if needed:
```bash
pip install --upgrade openai
```

---

## Performance Reference

| Scenario | Approx. time to first word |
|---|---|
| gpt-4o-mini, no web search | 0.3 – 1.0 s |
| gpt-4o, no web search | 0.5 – 2.0 s |
| gpt-4o, with web search | 2.0 – 5.0 s (searching first) |
| o1 / o1-mini | 5 – 20 s (reasoning model, slow by design) |

All times are network-dependent (OpenAI servers are overseas from your location).

---

## Folder Structure Added

```
AI Reasoning Agent/
├── workspace_tables.sql          ← Run in SSMS
├── workspace_db.py               ← Database layer
├── workspace_openai_service.py   ← OpenAI wrapper
├── workspace_bp.py               ← Flask Blueprint
│
├── templates/workspace/
│   ├── chat.html                 ← Main AI Workspace (ChatGPT-style)
│   ├── projects.html
│   ├── saved_chats.html
│   ├── prompt_library.html
│   ├── artifacts.html
│   ├── team.html
│   ├── memory.html
│   ├── activity.html
│   ├── analytics.html
│   ├── replay.html
│   ├── model_usage.html
│   └── settings.html
│
├── static/css/
│   └── workspace.css
│
├── static/js/
│   └── workspace_chat.js
│
└── AI_WORKSPACE_DOCS.md          ← This file
```

---

## What Was NOT Changed

These existing features are completely untouched:

- `auth.py` — authentication and session management
- `token_limits.py` — daily token budgets (workspace just adds a new call type)
- `nlq_engine.py` — BI agent NLQ engine
- `agent_manager.py` — BI agent management
- `dashboard_generator.py`, `reportgenerator.py`, `ppt_generator.py`
- All existing routes in `app.py`
- All existing templates (except sidebar which had items added)
- All existing CSS except new `workspace.css` added

The AI Workspace is entirely additive — a new Blueprint, new tables, new templates, new static files.
