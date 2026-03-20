"""Built-in tool registry and dispatcher for Hub Agents.

This module is the single source of truth for every "built-in" tool a Hub
Agent (see ``agents_hub_bp`` / ``HubOrchestrator``) can call during a
conversation or workflow run. It contains three things:

1. **Tool implementations** — plain Python functions (``web_search``,
   ``query_database``, ``send_email``, ``search_knowledge``, etc.) that do
   the actual work. Every tool function accepts ``**kwargs`` in addition to
   its named parameters because the orchestrator injects private,
   non-LLM-supplied context alongside the model-supplied arguments, e.g.:

   - ``api_key``     — provider API key for tools that call out to an LLM API.
   - ``_hub_ctx``     — dict with the current hub session/user context
     (``_hub_ctx['user']`` carries id/role/email used for permission checks).
   - ``_db_query``    — optional override for the SQL Server query helper.
   - tool-config keys configured per-agent in the agent editor (e.g.
     ``directories``, ``document_ids``, ``agent_names``, ``connector_keys``).

   Tool functions return a JSON-serialisable ``dict``. By convention this
   dict carries a ``"success"`` boolean and either result fields or an
   ``"error"``/``"warning"`` string, though a few legacy tools omit
   ``"success"`` and only set ``"error"`` on failure. Some tools instead
   ``raise`` (e.g. ``ValueError``/``RuntimeError``) when they want the
   orchestrator to see a retryable, LLM-actionable failure message —
   ``execute_tool`` catches these and folds them into the standard error
   shape.

2. **TOOL_REGISTRY** — a dict mapping tool name -> metadata (display name,
   category, human-readable description, the callable itself, an
   LLM-facing parameter ``schema``, and a display ``icon``). This is what
   the agent editor UI and the LLM tool-calling layer both read from to
   know which tools exist and how to call them. Custom, user-defined tools
   (stored in the ``hub_custom_tools`` DB table) are merged into this same
   dict at runtime via :func:`register_custom_tool` /
   :func:`unregister_custom_tool`, so callers never need to distinguish
   built-in vs. custom tools.

3. **Dispatch machinery** — :func:`execute_tool` is the single entry point
   the orchestrator calls with a tool name and parameter dict; it looks the
   tool up in ``TOOL_REGISTRY``, invokes its function, normalises the
   result/error shape, and records call/success counters in the
   ``hub_tools`` DB table.

Built-in tools fall into the following categories (matching the
``category`` field used in ``TOOL_REGISTRY`` and the agent editor UI):

- **Data Analysis** — ``query_database``, ``update_company_watchlist``,
  ``analyze_csv_files`` (read/write SQL access and local CSV/Excel
  exploration via pandas).
- **Document Management** — ``process_document``, ``search_knowledge``,
  ``search_connector_knowledge`` (RAG ingestion and retrieval over the
  vector knowledge base).
- **External Integration** — ``web_search``, ``call_external_api`` (calls
  out to the open internet / arbitrary REST APIs).
- **Communication** — ``send_email``, ``send_sms``,
  ``get_teams_chats_with_person``, ``get_outlook_emails`` (SMTP, Twilio,
  and Microsoft Graph integrations).
- **File Operations** — ``create_text_file``, ``load_text_file``,
  ``folder_report`` (read/write access to the agent's sandboxed file
  store).
- **Agent Collaboration** — ``communicate_with_agent``,
  ``communicate_with_data_agent`` (agent-to-agent and agent-to-BI-agent
  delegation).
- **System Monitoring** — ``check_system_status`` (CPU/memory/disk and
  hub activity metrics).
- **Utilities** — ``run_python_code`` (sandboxed Python execution),
  ``manage_knowledge`` (persistent key/value store).

Security note: ``update_company_watchlist`` is intentionally the only
write-capable database tool exposed to chat agents, and it can only affect
the ``[News].[CompanyNames]`` table; ``query_database`` is restricted to
read-only statement starters; ``run_python_code`` and the code-execution
path inside ``analyze_csv_files`` run against a restricted builtins/import
allowlist and a blocklist of dangerous substrings. These restrictions are
documented here as-is and must not be loosened without a deliberate
security review.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime


# ── Project root / tools-dir path setup ─────────────────────────────────────
# registry.py lives at agents/core/tools/registry.py; its sibling tool
# modules (db_tools.py, graph_tools.py, etc.) live in the same directory and
# are imported below as plain top-level modules (not a relative/dotted
# package import) so this works identically under both of this module's two
# load identities (see blueprints/agents_hub_bp.py's _PRELOAD list, and
# services/workspace_openai_service.py's normal `agents.core.tools.registry`
# import) without any changes to either loader.
_TOOLS_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_TOOLS_DIR, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# ── Tool implementations, relocated to sibling category modules ─────────────
from web_tools import web_search, call_external_api
from db_tools import (
    FETCH_ROW_CAP, query_database,
    WATCHLIST_CONNECTION_NAME, update_company_watchlist,
    analyze_csv_files,
)
from sandbox_tools import run_python_code, manage_knowledge
from file_tools import create_text_file, load_text_file, folder_report
from comm_tools import send_email, send_sms
from agent_collab_tools import communicate_with_agent, communicate_with_data_agent
from knowledge_tools import (
    search_documents, process_document, search_knowledge,
    search_connector_knowledge, list_connector_documents,
    list_knowledge_documents,
)
from system_tools import check_system_status
from graph_tools import get_teams_chats_with_person, get_outlook_emails


# ─── TOOL REGISTRY + DISPATCH ────────────────────────────────────────────────

TOOL_REGISTRY = {
    "web_search": {
        "display_name": "Web Search",
        "category":     "External Integration",
        "description":  "Live internet search powered by OpenAI web_search_preview (with DuckDuckGo fallback). Returns real-time answers with sources.",
        "function":     web_search,
        "schema": {
            "query": {"type": "string", "required": True,
                      "description": "What to search for — be specific for best results"},
        },
        "icon": "🌐",
    },
    "query_database": {
        "display_name": "Query Database",
        "category":     "Data Analysis",
        "description":  "Execute a SQL SELECT query against a configured database connection (SQL Server, PostgreSQL, MySQL, SQLite).",
        "function":     query_database,
        "schema": {
            "sql":             {"type": "string", "required": True,
                                "description": "SQL SELECT query to execute"},
            "connection_name": {"type": "string", "required": False,
                                "description": "Name of the database connection to use (uses first available if omitted)"},
        },
        "icon": "🗄️",
    },
    "update_company_watchlist": {
        "display_name": "Update Company Watchlist",
        "category":     "Data Analysis",
        "description":  (
            "Add or remove a company from [News].[CompanyNames] (the active company watchlist). "
            "action='add' inserts the company (or reactivates it if it already exists, setting is_removed='no'). "
            "action='remove' soft-deletes it by setting is_removed='yes'. "
            "Returns a verification result confirming the row's new state. "
            "This is the ONLY write-capable database tool, and it can only affect [News].[CompanyNames]."
        ),
        "function":     update_company_watchlist,
        "schema": {
            "action":            {"type": "string", "required": True,
                                  "description": "'add' or 'remove'"},
            "new_client":        {"type": "string", "required": True,
                                  "description": "Company name to add or remove"},
            "reference_company": {"type": "string", "required": False,
                                  "description": "Reference client name (looked up from the lead tables). "
                                                  "Pass 'None' if no reference was found. "
                                                  "On 'remove', if provided, narrows the match to this reference too."},
            "connection_name":   {"type": "string", "required": False,
                                  "description": "Name of the database connection to use (uses first available if omitted)"},
        },
        "icon": "✅",
    },
    "analyze_csv_files": {
        "display_name": "Analyze CSV / Excel Files",
        "category":     "Data Analysis",
        "description":  (
            "Load CSV or Excel files from configured directories and run pandas analysis. "
            "Step 1: call with preview_schema=True to inspect columns, dtypes, and sample rows. "
            "Step 2: call with a 'code' string — 'df' holds all files combined, "
            "'dfs' is a dict of {filename: DataFrame}. "
            "Store your answer in a variable named 'result'. "
            "Configured directories (set per-agent) are searched automatically, "
            "including their archive/ subdirectories."
        ),
        "function":     analyze_csv_files,
        "schema": {
            "pattern":        {"type": "string",  "required": False,
                               "description": "Glob pattern to match filenames, e.g. 'GA_*.csv'"},
            "files":          {"type": "array",   "required": False,
                               "description": "Explicit list of filenames to load"},
            "preview_schema": {"type": "boolean", "required": False,
                               "description": "Return column names, dtypes and 5 sample rows without running code"},
            "code":           {"type": "string",  "required": False,
                               "description": "Pandas code to execute. 'df' = combined DataFrame, 'dfs' = per-file dict. Assign result to a variable named 'result'."},
        },
        "icon": "📊",
    },
    "run_python_code": {
        "display_name": "Run Python Code",
        "category":     "Utilities",
        "description":  "Execute sandboxed Python code. Allowed modules: json, math, re, datetime, collections, itertools, statistics, random.",
        "function":     run_python_code,
        "schema": {
            "code": {"type": "string", "required": True,
                     "description": "Python code to execute. Use print() to capture output."},
        },
        "icon": "🐍",
    },
    "create_text_file": {
        "display_name": "Create Text File",
        "category":     "File Operations",
        "description":  "Create or overwrite a text file in the agent file store. Returns the filename for use with load_text_file.",
        "function":     create_text_file,
        "schema": {
            "filename": {"type": "string", "required": True,
                         "description": "Filename (e.g. report.txt). No path — file is stored in the agent store."},
            "content":  {"type": "string", "required": True,
                         "description": "Text content to write"},
        },
        "icon": "📝",
    },
    "load_text_file": {
        "display_name": "Load Text File",
        "category":     "File Operations",
        "description":  "Read a text file previously created with create_text_file.",
        "function":     load_text_file,
        "schema": {
            "filename": {"type": "string", "required": True,
                         "description": "Filename to read (e.g. report.txt)"},
        },
        "icon": "📂",
    },
    "send_email": {
        "display_name": "Send Email",
        "category":     "Communication",
        "description":  "Send an email via SMTP. Requires SMTP_HOST, SMTP_USER, SMTP_PASS env vars.",
        "function":     send_email,
        "schema": {
            "to":      {"type": "string",  "required": True,
                        "description": "Recipient email address"},
            "subject": {"type": "string",  "required": True,
                        "description": "Email subject line"},
            "body":    {"type": "string",  "required": True,
                        "description": "Email body (plain text by default)"},
            "cc":      {"type": "string",  "required": False,
                        "description": "CC email address (optional)"},
            "html":    {"type": "boolean", "required": False,
                        "description": "Set true to send body as HTML"},
        },
        "icon": "📧",
    },
    "send_sms": {
        "display_name": "Send SMS",
        "category":     "Communication",
        "description":  "Send an SMS via Twilio. Requires TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM env vars.",
        "function":     send_sms,
        "schema": {
            "to":      {"type": "string", "required": True,
                        "description": "Recipient phone number in E.164 format (e.g. +14155552671)"},
            "message": {"type": "string", "required": True,
                        "description": "SMS message body (max 160 chars for single SMS)"},
        },
        "icon": "💬",
    },
    "call_external_api": {
        "display_name": "Call External API",
        "category":     "External Integration",
        "description":  "Make a real HTTP request (GET/POST/PUT/DELETE) to any external REST API and return the response.",
        "function":     call_external_api,
        "schema": {
            "url":     {"type": "string", "required": True,
                        "description": "Full URL to call"},
            "method":  {"type": "string", "required": False,
                        "description": "HTTP method: GET, POST, PUT, DELETE (default: GET)"},
            "payload": {"type": "object", "required": False,
                        "description": "JSON body for POST/PUT requests"},
            "headers": {"type": "object", "required": False,
                        "description": "Extra HTTP headers as key-value object"},
        },
        "icon": "🔗",
    },
    "process_document": {
        "display_name": "Process Document",
        "category":     "Document Management",
        "description":  "Ingest a document file into the vector knowledge base (RAG). Use this when a user attaches a file and wants it indexed for future searches. The file_path is provided automatically by the chat when the user uploads a file with 'Add to Knowledge Base' mode.",
        "function":     process_document,
        "schema": {
            "file_path":   {"type": "string", "required": True,
                            "description": "Server-side path to the file to ingest"},
            "source_name": {"type": "string", "required": False,
                            "description": "Display name for the document"},
        },
        "icon": "📥",
    },
    "manage_knowledge": {
        "display_name": "Manage Knowledge",
        "category":     "Utilities",
        "description":  "Persistently store and retrieve key-value data across agent sessions.",
        "function":     manage_knowledge,
        "schema": {
            "action": {"type": "string", "required": True,
                       "enum": ["get", "set", "delete", "list"],
                       "description": "Action: get (retrieve), set (store), delete (remove), list (all keys)"},
            "key":    {"type": "string", "required": True,
                       "description": "Key name to get/set/delete"},
            "value":  {"type": "string", "required": False,
                       "description": "Value to store (required for 'set' action)"},
        },
        "icon": "🧠",
    },
    "search_knowledge": {
        "display_name": "Search Knowledge Base (RAG)",
        "category":     "Document Management",
        "description":  "Retrieve relevant content from uploaded documents using semantic search (RAG). Call this BEFORE answering substantive questions that may be covered by documents in the knowledge base — but skip it for greetings/small talk/chit-chat that needs no lookup. Returns a 'context' field with formatted source excerpts ready to use in your answer — always cite the source name.",
        "function":     search_knowledge,
        "schema": {
            "query":        {"type": "string",  "required": True,
                             "description": "Natural-language question or keywords to search for"},
            "n_results":    {"type": "integer", "required": False,
                             "description": "Maximum number of results to return (default 5)"},
            "document_ids": {"type": "array",   "required": False,
                             "description": "Restrict search to specific document IDs (configured per-agent)"},
        },
        "icon": "🔎",
    },
    "search_connector_knowledge": {
        "display_name": "Search Connector Knowledge",
        "category":     "Document Management",
        "description":  (
            "Full-text search across documents ingested from a connector. "
            "Without filters, searches all connector documents using vector similarity — works standalone. "
            "For follow-up questions about a specific person or document, pass their name in "
            "filter_document_name (e.g. 'Pranil Bhilare') — the tool resolves the document internally. "
            "If list_connector_documents is also available, prefer calling it first to rank all docs, "
            "then use filter_document_name here to deep-search a specific result."
        ),
        "function":     search_connector_knowledge,
        "schema": {
            "query":          {"type": "string",  "required": True,
                               "description": "What to look for inside the document (e.g. 'education, location, work experience')"},
            "n_results":      {"type": "integer", "required": False,
                               "description": "Maximum results to return (default 5)"},
            "connector_keys": {"type": "array",   "required": False,
                               "description": "List of connector keys — pre-filled by agent config"},
            "filter_document_name": {"type": "string", "required": False,
                               "description": "Name of the specific candidate or document to search within (e.g. 'Pranil Bhilare'). Always use this for follow-up questions about a named person or document — never guess a doc_id."},
            "filter_doc_ids": {"type": "array",   "required": False,
                               "description": "Advanced: exact doc_id strings from list_connector_documents. Prefer filter_document_name unless you have confirmed doc_id values."},
        },
        "icon": "📂",
    },
    "list_connector_documents": {
        "display_name": "List Connector Documents",
        "category":     "Document Management",
        "description":  (
            "Lists all documents in a connector with their summaries and key metrics. "
            "Pass a query to rank every document by relevance and return only the top matches — "
            "useful for ranking candidates against a JD or finding the most relevant files. "
            "Works standalone to present a ranked summary list. "
            "If search_connector_knowledge is also available, follow up with it using "
            "filter_document_name to retrieve full details on a specific result."
        ),
        "function":     list_connector_documents,
        "schema": {
            "connector_keys": {"type": "array", "required": False,
                               "description": "List of connector keys (e.g. ['sharepoint:5']) — pre-filled by agent config"},
            "query":          {"type": "string", "required": False,
                               "description": "Optional — rank all documents by similarity to this text (e.g. the JD) and return only the top matches"},
            "n_results":      {"type": "integer", "required": False,
                               "description": "Max documents to return when query is given (default 20)"},
        },
        "icon": "📋",
    },
    "list_knowledge_documents": {
        "display_name": "List Knowledge Documents",
        "category":     "Document Management",
        "description":  "List directly-attached knowledge documents with their summaries and key metrics. Every document is checked; pass 'query' to rank by relevance and return only the top matches instead of the full list. Use this as Stage 1 to identify which documents are relevant before doing a deep search.",
        "function":     list_knowledge_documents,
        "schema": {
            "document_ids": {"type": "array", "required": False,
                             "description": "Doc IDs to list — pre-filled by agent config if omitted"},
            "query":        {"type": "string", "required": False,
                             "description": "Optional — rank all documents by similarity to this text and return only the top matches"},
            "n_results":    {"type": "integer", "required": False,
                             "description": "Max documents to return when query is given (default 20)"},
        },
        "icon": "📋",
    },
    "get_teams_chats_with_person": {
        "display_name": "Teams Chat Reader",
        "category":     "Communication",
        "description":  (
            "Fetch recent Microsoft Teams messages between the current user and another person. "
            "Searches both the 1:1 chat and group chats, returns a merged message pool newest-first. "
            "Requires TENANT_ID, BOT_APP_ID, BOT_APP_PASSWORD env vars and "
            "Chat.Read.All application permission with admin consent."
        ),
        "function":     get_teams_chats_with_person,
        "schema": {
            "target":          {"type": "string",  "required": True,
                                "description": "Email or display name of the person to fetch chats with (e.g. 'zaid' or 'zaid@company.com')"},
            "pool_size":       {"type": "integer", "required": False,
                                "description": "Number of messages to return (default 30). Set to ~2x what the user wants."},
            "include_groups":  {"type": "boolean", "required": False,
                                "description": "Set true only when the user explicitly asks about group chats or says 'all chats'. Default false (1:1 only)."},
            "group_name":      {"type": "string",  "required": False,
                                "description": "Name of a specific group chat to search (e.g. 'Nexus'). Automatically enables include_groups."},
            "search_keyword":  {"type": "string",  "required": False,
                                "description": "Keyword to search for across message history (e.g. 'budget'). Scans up to 500 messages deep instead of just recent ones."},
        },
        "icon": "💬",
    },
    "get_outlook_emails": {
        "display_name": "Outlook Email Reader",
        "category":     "Communication",
        "description":  (
            "Read emails from the current user's own Outlook mailbox via Microsoft Graph. "
            "Supports filtering by sender, subject/body keywords, date range, folder, and read status. "
            "Only accesses the calling user's mailbox — never another user's. "
            "Requires TENANT_ID, BOT_APP_ID, BOT_APP_PASSWORD env vars and "
            "Mail.Read.All application permission with admin consent."
        ),
        "function":     get_outlook_emails,
        "schema": {
            "from_address":     {"type": "string",  "required": False,
                                 "description": "Filter by sender — accepts email address (e.g. 'alice@company.com') or display name (e.g. 'Alice Smith')."},
            "subject_keyword":  {"type": "string",  "required": False,
                                 "description": "Search for this keyword in the email subject line (e.g. 'invoice')."},
            "body_keyword":     {"type": "string",  "required": False,
                                 "description": "Search for this keyword in the email body (e.g. 'project deadline')."},
            "folder":           {"type": "string",  "required": False,
                                 "description": "Mailbox folder to search. Omit this for an open-ended request like 'what's my latest email' — it then searches every folder in the mailbox (including custom folders inbox rules route mail into), excluding Sent/Drafts/Deleted/Junk. Pass a value only to scope the search: well-known options 'inbox', 'sent', 'drafts', 'deleted', 'archive', 'junk' — or the exact name of a custom folder (e.g. a client's name), including folders nested under Inbox."},
            "max_results":      {"type": "integer", "required": False,
                                 "description": "Number of emails to return (1–50, default 20). Set higher for broader searches."},
            "unread_only":      {"type": "boolean", "required": False,
                                 "description": "Set true to return only unread emails."},
            "date_from":        {"type": "string",  "required": False,
                                 "description": "Start of date range as YYYY-MM-DD (e.g. '2024-01-15'). Only emails received on or after this date are returned."},
            "date_to":          {"type": "string",  "required": False,
                                 "description": "End of date range as YYYY-MM-DD (e.g. '2024-01-31'). Only emails received on or before this date are returned."},
        },
        "icon": "📧",
    },
    "communicate_with_agent": {
        "display_name": "Communicate With Agent",
        "category":     "Agent Collaboration",
        "description":  "Send a message to another hub agent and receive its response. Use agent UUID or exact agent name.",
        "function":     communicate_with_agent,
        "schema": {
            "agent_id": {"type": "string", "required": True,
                         "description": "Target agent UUID or exact name"},
            "message":  {"type": "string", "required": True,
                         "description": "Message to send to the agent"},
        },
        "icon": "🤝",
    },
    "communicate_with_data_agent": {
        "display_name": "Communicate With Data Agent",
        "category":     "Agent Collaboration",
        "description":  "Send a natural-language question to a BI data agent. The agent queries its connected database and returns the answer, data rows, and SQL used.",
        "function":     communicate_with_data_agent,
        "schema": {
            "agent_name": {"type": "string", "required": True,
                           "description": "Exact name of the BI data agent to query"},
            "question":   {"type": "string", "required": True,
                           "description": "Natural-language question for the data agent to answer"},
        },
        "icon": "🗄️",
    },
    "check_system_status": {
        "display_name": "Check System Status",
        "category":     "System Monitoring",
        "description":  "Get real-time system health metrics: CPU, memory, disk usage, active agents, uptime.",
        "function":     check_system_status,
        "schema":       {},
        "icon": "📡",
    },
    "folder_report": {
        "display_name": "Folder Report",
        "category":     "File Operations",
        "description":  "List contents and statistics for a directory. Defaults to the agent file store.",
        "function":     folder_report,
        "schema": {
            "path": {"type": "string", "required": False,
                     "description": "Directory path to inspect (leave empty for agent file store)"},
        },
        "icon": "📁",
    },
}


def register_custom_tool(tool_row: dict) -> None:
    """Register a ``hub_custom_tools`` DB row into ``TOOL_REGISTRY`` at runtime.

    The registered wrapper delegates to ``agents_hub_bp._exec_custom_tool``
    (resolved via ``sys.modules`` at call time, not import time) so it
    benefits from venv isolation and env var injection. If the
    ``agents_hub_bp`` blueprint module isn't loaded yet (e.g. during early
    app startup), falls back to a direct ``exec()`` of the stored
    ``imports_code`` + ``function_code``, which must define a callable
    named ``run``.

    Args:
        tool_row (dict): A row from the ``hub_custom_tools`` table.
            Expected keys: ``name`` (required, used as the registry key),
            ``display_name``, ``category``, ``description``,
            ``input_schema`` (JSON string encoding a list of
            ``{"name", "type", "required", "description"}`` param specs),
            ``imports_code``, ``function_code``, ``pip_packages``,
            ``env_vars_json``, ``venv_path``.

    Returns:
        None. Mutates the module-level ``TOOL_REGISTRY`` dict in place,
        inserting/overwriting the entry for ``tool_row["name"]`` with
        ``"custom": True``.
    """
    name             = tool_row["name"]
    input_schema_raw = json.loads(tool_row.get("input_schema") or "[]")

    schema = {}
    for p in input_schema_raw:
        schema[p["name"]] = {
            "type":        p.get("type", "string"),
            "required":    p.get("required", False),
            "description": p.get("description", ""),
        }

    # Snapshot the row fields needed at call-time (captures current code/env/venv)
    _snapshot = {k: tool_row.get(k) for k in
                 ("name", "imports_code", "function_code",
                  "pip_packages", "env_vars_json", "venv_path")}

    def _make_fn(snapshot):
        """Build a closure-bound callable for one custom tool, capturing its row snapshot."""
        def _custom_tool_fn(**kwargs):
            """Invoke the custom tool via `agents_hub_bp._exec_custom_tool`, or exec() as fallback.

            Args:
                **kwargs: Model-supplied tool parameters plus
                    orchestrator-injected keys (``_hub_ctx``, ``api_key``,
                    ``_db_query``) — the latter two are stripped before
                    being passed to the custom tool's own code.

            Returns:
                dict: Whatever the custom tool's ``run()``/execution
                returns, or ``{"error": str}`` if ``function_code`` does
                not define a callable named ``run`` (fallback path only).
            """
            hub_bp = sys.modules.get("agents_hub_bp")
            if hub_bp and hasattr(hub_bp, "_exec_custom_tool"):
                hub_ctx      = kwargs.pop("_hub_ctx", {})
                # Strip orchestrator-injected kwargs that aren't real params
                clean_params = {k: v for k, v in kwargs.items()
                                if k not in ("api_key", "_db_query")}
                return hub_bp._exec_custom_tool(snapshot, clean_params, hub_ctx)
            # Fallback: exec() path (used during startup before bp is registered)
            ns = {}
            imp = snapshot.get("imports_code") or ""
            fn  = snapshot.get("function_code") or "def run(**kwargs):\n    return {}"
            if imp.strip():
                exec(imp, ns)
            exec(fn, ns)
            run_fn = ns.get("run")
            if not callable(run_fn):
                return {"error": "function_code must define a callable named 'run'"}
            return run_fn(**kwargs)
        return _custom_tool_fn

    TOOL_REGISTRY[name] = {
        "display_name": tool_row.get("display_name", name),
        "category":     tool_row.get("category", "Custom"),
        "description":  tool_row.get("description", ""),
        "function":     _make_fn(_snapshot),
        "schema":       schema,
        "icon":         "🔧",
        "custom":       True,
    }


def unregister_custom_tool(name: str) -> None:
    """Remove a custom tool from ``TOOL_REGISTRY`` by name, if present.

    Args:
        name (str): Tool name (registry key) to remove.

    Returns:
        None. No error is raised if ``name`` isn't currently registered.
    """
    TOOL_REGISTRY.pop(name, None)


def execute_tool(tool_name: str, params: dict) -> dict:
    """Look up and invoke a tool by name, normalising its result/error shape.

    This is the single dispatch entry point the Hub orchestrator calls
    for every tool invocation (built-in or custom). It looks ``tool_name``
    up in ``TOOL_REGISTRY``, checks the tool's ``enabled`` flag, calls its
    underlying function with ``params`` as keyword arguments, wraps a
    successful call's return value in a standard envelope, and — on a
    best-effort basis — records the call (and whether it succeeded) in
    the ``hub_tools`` DB table for usage tracking. Note that the
    individual tool functions usually already return their own
    ``{"success": ...}``-shaped dict; this wrapper nests that under
    ``result`` rather than replacing it, so callers should generally
    inspect ``response["result"]`` for the tool's own success/error
    fields in addition to the outer ``response["success"]``.

    Args:
        tool_name (str): Key into ``TOOL_REGISTRY`` identifying which tool
            to run.
        params (dict): Keyword arguments to pass to the tool's underlying
            function (typically the model-supplied tool-call arguments,
            merged with orchestrator-injected context by the caller
            before this is invoked).

    Returns:
        dict: On success, ``{"success": True, "tool": str, "result":
        Any}`` where ``result`` is whatever the tool function returned.
        On failure, ``{"success": False, "tool": str, "error": str}`` —
        e.g. unknown tool name, tool disabled, invalid/missing parameters
        (``TypeError`` from the call), or any other exception raised by
        the tool. Unknown-tool and disabled-tool cases instead return a
        bare ``{"error": str}`` without the ``"success"``/``"tool"`` keys
        (legacy shape, preserved as-is).
    """
    if tool_name not in TOOL_REGISTRY:
        return {"error": f"Unknown tool: '{tool_name}'. "
                        f"Available: {list(TOOL_REGISTRY.keys())}"}
    tool = TOOL_REGISTRY[tool_name]
    if not tool.get('enabled', True):
        return {"error": f"Tool '{tool_name}' is currently disabled."}
    try:
        result = tool['function'](**params)
        result = {"success": True, "tool": tool_name, "result": result}
    except TypeError as e:
        result = {"success": False, "tool": tool_name,
                  "error": f"Invalid parameters: {e}"}
    except Exception as e:
        result = {"success": False, "tool": tool_name, "error": str(e)}

    try:
        from app_db import get_app_db as _get_app_db
        success_inc = 1 if result.get("success") else 0
        _conn = _get_app_db()
        _conn.cursor().execute(
            "UPDATE hub_tools SET total_calls=total_calls+1, success_calls=success_calls+? WHERE name=?",
            (success_inc, tool_name)
        )
        _conn.commit()
        _conn.close()
    except Exception:
        pass

    return result
