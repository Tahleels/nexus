# Nexus AI Portal — Architecture Overview

A study guide for explaining this project, not user-facing documentation.
For exhaustive schema/deployment detail see `docs/DATABASE_DOCUMENTATION.md`
and `docs/DEPLOYMENT_AND_MIGRATION.md` — this file is the "why does it look
like this" layer on top of those.

## 1. What it is, in one paragraph

A single Flask app that gives an organization two different flavors of AI
agent on top of the same infrastructure: **BI agents** that turn natural
language into SQL against a company database (NLQ → SQL → dashboard/report/
PPT), and **Hub agents** — general-purpose LLM agents with tools, multi-step
workflows, and human-in-the-loop approvals. Both share one SQL Server
database for everything: app state, user auth, document/RAG storage, and
vector embeddings. There is deliberately no separate vector DB, graph DB, or
job queue — those are all implemented in-process against SQL Server, with a
documented migration path to swap each one for its Azure equivalent later
(`docs/DEPLOYMENT_AND_MIGRATION.md` §5).

## 2. The one big design decision: no extra infra

`docs/DEPLOYMENT_AND_MIGRATION.md` is explicit that ChromaDB and Neo4j were
evaluated and **designed out** in favor of SQL Server + in-Python
implementations:

- **Vector search** → `agents/core/knowledge/vector_store.py`. Chunks and
  their embeddings live in the `app_document_chunks` table (embedding =
  JSON-serialized float array). Search is a brute-force cosine similarity
  loop in Python (`sklearn.metrics.pairwise.cosine_similarity`) over rows
  pulled from SQL — no ANN index. Fine below tens of thousands of chunks;
  the documented Azure AI Search migration exists for when that stops
  being true.
- **Schema join-graph** → `nlq/schema_graph.py`. A `NetworkXSchemaGraph`
  class rebuilds a `networkx.DiGraph` per BI agent from rows in
  `app_schema_graph` (table_a/table_b/join_key/weight) on each use, instead
  of a live graph DB. **Correction to the "replaced Neo4j" framing**: the
  Neo4j-backed `SchemaGraph` class is still fully implemented and is tried
  *first* by the factory `get_schema_graph()`; it only falls through to
  NetworkX when `NEO4J_URI` isn't set. In practice, with no Neo4j env vars
  configured (the normal case), NetworkX is what actually runs.

Interview framing: this is a good example of picking boring, already-paid-for
infrastructure (one SQL Server) over adding three more moving parts, with
an explicit "when this stops being enough, here's exactly how to swap it"
plan rather than premature optimization.

## 3. Two agent systems, genuinely different architectures

| | BI Agents (NLQ) | Hub Agents |
|---|---|---|
| Purpose | NL question → SQL → chart/report/PPT | General-purpose LLM agent + tools + workflows |
| Core file | `nlq/nlq_engine.py` | `agents/core/orchestrator/engine.py` |
| LLM integration | **LangChain** — `ChatOpenAI`/`ChatBedrockConverse` (via `llm_providers.langchain_factory`) + `create_sql_query_chain` | **`llm_providers/`** — a provider-agnostic abstraction (OpenAI + Amazon Bedrock) that replaced the old hand-rolled `urllib.request` calls to `api.openai.com` |
| Data source | Customer/BI databases via `database/database_manager.py` (SQLAlchemy pool, separate from the app's own DB) | Tool calls (`agents/core/tools/registry.py`, ~3.7k lines: web_search, query_database, send_email, search_knowledge, ...) |
| Extra machinery | Schema-embedding pruning (`hybrid_schema_pruning()`) + join-path graph (§2) so the LLM isn't handed the whole schema | Workflow engine (`agents/core/workflows/engine.py`) for multi-step chains; HITL approvals (`hub_approvals` table) |

These two systems were clearly built at different times / for different
reasons and never unified — worth knowing so you're not surprised that
"how does the LLM call work" has two unrelated answers depending which
half of the app you're in. Both answers now go through the same
**provider selection** underneath (`DEFAULT_LLM_PROVIDER` / a per-agent or
per-conversation `provider` column / an explicit override), just via two
different bridging modules for two different underlying frameworks:
`llm_providers.factory` (the direct/streaming path — orchestrator,
generators, workspace chat) and `llm_providers.langchain_factory` (the
LangChain path — NLQ only). See `llm_providers/__init__.py`'s docstring.

## 4. Where the heavy ML libraries (torch, transformers, etc.) actually get used

Short version: **torch and transformers are never imported directly** —
they're transitive dependencies pulled in by `sentence-transformers` and
`easyocr`. The actual usage is narrow and specific:

| Library | Where | What for |
|---|---|---|
| `sentence-transformers` (`all-MiniLM-L6-v2`, 384-dim) | `agents/core/knowledge/vector_store.py` `_get_model()` | Embeds document chunks for RAG search (lazy-loaded, module-level singleton) |
| `sentence-transformers` (same model, **independent instance**) | `nlq/nlq_engine.py` `get_embedding_model()` | Embeds table/column schema text for `hybrid_schema_pruning()` — picking relevant tables before SQL generation. Cached in `app_schema_embedding_cache` so it isn't recomputed every restart |
| `sentence-transformers` `CrossEncoder` (`cross-encoder/ms-marco-MiniLM-L-6-v2`) | `agents/core/knowledge/rag_pipeline.py` `_get_reranker()` | Optional stage-2 reranking of hybrid (BM25 + vector) RAG results. Soft-fails to "no rerank" if unavailable |
| `easyocr` | `agents/core/knowledge/document_processor.py` `_extract_image()` | OCR for image ingestion (`.png/.jpg/...`), CPU-only, English, optional (soft-fails if not installed) |
| `networkx` | `nlq/schema_graph.py` | Join-path graph, see §2 |

**Two separate embedding subsystems using the same model** (RAG chunk
search vs. NLQ schema pruning) is a detail worth calling out — they don't
share a cache or a loaded instance, so the model gets loaded twice into
memory.

## 5. Dependencies in `requirements.txt` that are vestigial (not used)

Worth knowing so you don't over-explain a library that isn't actually load-bearing:

| Package | Evidence |
|---|---|
| `chromadb` | No `import chromadb` anywhere. Only survives as stale comments in `document_processor.py` ("embed + store chunks in ChromaDB") — the real store is SQL Server via `vector_store.py` |
| `msoffcrypto-tool`, `oletools` | No imports anywhere. Legacy `.doc` files are instead converted via a shelled-out headless LibreOffice (`soffice.exe`) call, then parsed as `.docx` |
| `langchain-anthropic`, `anthropic` SDK | Still true post-Bedrock: no client calls Anthropic directly. Claude model names only appear in `core/token_limits.py`'s **pricing table** (cost-estimation lookup) and as *model ids you can select on Bedrock* (`anthropic.claude-sonnet-5`, etc.) — Anthropic's own API remains unwired; multi-provider support landed via `llm_providers/` (OpenAI + Bedrock) instead |

## 6. Directory map

| Dir | Role | Key files |
|---|---|---|
| `app.py` | Entry point — not a factory pattern, module-level `app = Flask(__name__)`. Registers blueprints, owns most BI/admin routes directly | — |
| `agents/` | Hub agent subsystem | `core/orchestrator/engine.py` (LLM calls via `llm_providers`), `core/tools/registry.py` (tool catalog), `core/workflows/engine.py`, `core/knowledge/{document_processor,rag_pipeline,vector_store}.py` |
| `blueprints/` | Flask Blueprints registered by `app.py` | `agents_hub_bp.py` (Hub UI/API, **own second APScheduler instance** for tool jobs), `workspace_bp.py` (multi-model chat UI — OpenAI + Bedrock as of `llm_providers`), `knowledge_bp.py`, `training_bp.py`, `admin_conversations_bp.py`, `app_jobs_routes.py` |
| `core/` | Cross-cutting infra | `auth.py` (bcrypt + server-side sessions, no JWT), `token_limits.py` (daily LLM token budget w/ live pricing feed), `logging_config.py` |
| `database/` | All SQL Server access — one shared pyodbc connection pattern | `app_db.py` (`get_app_db()`, `ensure_tables()` — the single source of the connection string), `database_manager.py` (separate SQLAlchemy pool for *customer* BI databases), `org_db.py` (department/project scoping + guardrails), `nexus_sync_db.py` (cross-DB views into an external portal) |
| `generators/` | BI output generation, calling LLMs via `generator_utils.provider_chat()` | `dashboard_generator.py`, `reportgenerator.py`, `infographicgenerator.py`, `ppt_generator.py` |
| `llm_providers/` | Provider-agnostic LLM abstraction (OpenAI + Amazon Bedrock) | `base.py` (interface), `openai_provider.py`, `bedrock_provider.py` (Bedrock `Converse`/`ConverseStream` API), `factory.py` (provider resolution), `langchain_factory.py` (bridges to LangChain's own `BaseChatModel` for `nlq_engine.py`) |
| `nlq/` | NL→SQL engine | `nlq_engine.py`, `schema_graph.py` (§2/§3) |
| `services/` | Background services + BI agent CRUD | `scheduler_service.py` (APScheduler for BI jobs), `agent_manager.py`, `document_watcher.py` / `sharepoint_watcher.py` (auto-ingest into RAG) |

## 7. Auth & multi-tenancy model

Three roles (`admin` / `dev` / `user`) in a `roles` table, bcrypt password
hashes, server-side session tokens (`user_sessions`) rather than JWTs.
Access is scoped through `departments` / `projects`, and BI-agent access is
further restricted per user/department/project via `agent_guardrails` —
JSON rule sets that get merged (union of filters, intersection of allowed
tables) and injected into the NLQ prompt so the same agent can't leak rows
outside a user's scope. See `docs/DATABASE_DOCUMENTATION.md` §1–2 for the
full table shapes.

## 8. Scheduling — two independent APScheduler instances

Not unified: `services/scheduler_service.py` runs BI dashboard/report/PPT
jobs (`app_jobs` table), started explicitly in `app.py`'s `if __name__ ==
'__main__':` block — meaning it does **not** start if the app is imported
under a WSGI server that doesn't also run that block. Separately,
`blueprints/agents_hub_bp.py` instantiates its *own* `BackgroundScheduler`
at module import time for Hub "tool" jobs (`hub_jobs` table). Two schedulers,
two job tables, started at two different points in the app lifecycle — a
natural follow-up question is why these weren't unified.

## 9. Startup sequence (`app.py`, executed at **import** time, before Flask exists)

1. Add `core/database/services/generators/nlq/blueprints` to `sys.path` (flat imports)
2. `load_dotenv()`
3. `app_db.ensure_tables()` → `migrate_from_json()` → `ensure_portal_views()` (no-ops unless `PORTAL_DB_NAME` is configured) → `workspace_db.ensure_schema()`
4. Logging setup
5. Background watcher threads started best-effort (document + SharePoint watchers, each try/except-wrapped so a missing dependency only logs a warning)
6. Flask app object created, blueprints registered, Sentry/Teams alerting wired up
7. Only under direct `python app.py`: `scheduler_service.start()` then `app.run()`

## 10. This local dev setup

Running against a fresh, empty local SQL Server database (`nexus_local_dev`
on the local `SQLEXPRESS` instance) — **not** the real production database or
its `.bak` backup. `ensure_tables()` builds the app-internal schema
automatically on first startup; the `sql/*.sql` scripts build the rest
(auth, hub, workspace, training tables). No real production data, API keys, or
credentials are used by this local instance.
