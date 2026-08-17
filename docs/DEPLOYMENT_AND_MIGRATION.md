# Deployment & Migration Guide

## Table of Contents
1. [Local NSSM Deployment — Steps After These Code Changes](#1-local-nssm-deployment)
2. [New pip Dependencies to Install](#2-new-pip-dependencies)
3. [Environment Variables](#3-environment-variables)
4. [First-Run Verification Checklist](#4-first-run-verification-checklist)
5. [Azure Migration Playbook](#5-azure-migration-playbook)

---

## 1. Local NSSM Deployment

These are the only steps required after pulling the new code.

### 1.1 Install new Python packages

Open PowerShell **as Administrator** inside the project directory:

```powershell
venv\Scripts\pip install pdfplumber python-docx beautifulsoup4 extract-msg
```

Optional (only if you need image/OCR ingestion):
```powershell
venv\Scripts\pip install easyocr
# No binary install needed — uses PyTorch (already in requirements).
# First OCR call downloads ~100 MB of model files to %USERPROFILE%\.EasyOCR\
# Add more languages via: easyocr.Reader(['en','ar'], gpu=False) in document_processor.py
```

> **Already in requirements (no action needed):** `python-pptx`, `openpyxl`, `pandas`,
> `sentence-transformers`, `networkx`, `scikit-learn`
>
> **chromadb is NOT required.** Document chunks and knowledge embeddings are stored
> directly in SQL Server (`app_document_chunks`, `app_knowledge_store` tables).

### 1.2 SQL Server — new tables

The app creates all tables automatically on startup via `ensure_tables()`.
Three new tables will be created if they don't exist:

| Table | Purpose |
|---|---|
| `app_schema_embedding_cache` | Persistent schema embeddings (no re-embed on restart) |
| `app_documents` | Metadata for ingested documents |
| `app_schema_graph` | Graph edges for join-path discovery (replaces Neo4j) |

No manual SQL is needed. If you want to verify after first startup:
```sql
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME IN ('app_schema_embedding_cache','app_documents','app_schema_graph');
-- Should return 3 rows
```

### 1.3 Vector storage location

Document chunks and knowledge embeddings are stored in **SQL Server** in the
`app_document_chunks` and `app_knowledge_store` tables — the same database
you already use. No SQLite files, no extra services.

Raw uploaded documents are saved to:
```
Data\agent_store\raw_documents\
```

### 1.4 Neo4j

**Skip it.** Do not set `NEO4J_URI` in `.env`. The app now uses
`NetworkXSchemaGraph` (SQL Server + NetworkX) automatically when Neo4j
credentials are absent. All graph features work without a separate server.

### 1.5 Restart the service

```powershell
Restart-Service NexusApp
Get-Service NexusApp   # Should show: Running
```

### 1.6 Verify startup logs

```powershell
Get-Content service_stderr.log -Wait -Tail 50
```

Look for these lines (order may vary):
```
app_db: tables verified/created
VectorStore: using SQL Server backend
NetworkXSchemaGraph: using NetworkX + SQL Server backend
Schema embedding cache: loaded N entries from DB
NLQ Engine initialized (hybrid pruning + per-agent learning)
Warmup complete
```

---

## 2. New pip Dependencies

| Package | Purpose | Required |
|---|---|---|
| `pdfplumber` | PDF text extraction | Yes (for PDF ingestion) |
| `python-docx` | Word document extraction | Yes (for .docx ingestion) |
| `beautifulsoup4` | HTML extraction | Yes (for HTML ingestion) |
| `extract-msg` | Outlook .msg email extraction | Yes (for .msg ingestion) |
| `easyocr` | Image OCR — no binary needed, PyTorch-based | Optional |
| `azure-search-documents` | Azure AI Search (migration step 3 only) | No |

Chunks and embeddings live in **SQL Server** — no vector DB needed.

Add to `requirements.txt`:
```
pdfplumber
python-docx
beautifulsoup4
extract-msg
```

---

## 3. Environment Variables

Add these to `.env` if needed (all optional):

```env
# Shared file/vector store root (supports UNC paths for network shares)
# Default: Data/agent_store/  relative to project dir
AGENT_FILE_STORE=\\fileserver\share\nexus_agent_store

# Neo4j (leave UNSET to use NetworkX/SQL Server graph — recommended for local)
# NEO4J_URI=bolt://localhost:7687
# NEO4J_USER=neo4j
# NEO4J_PASSWORD=yourpassword
```

---

## 4. First-Run Verification Checklist

After restarting the service, verify each component:

### 4.1 Document ingestion
Place a test PDF in `Data\agent_store\raw_documents\` and call the tool:
```python
# From Python console or agent tool call
from agents.core.knowledge.document_processor import process_document
result = process_document("test.pdf", source_name="Test Document")
print(result)
# Expected: {"success": True, "document_id": "doc_...", "chunk_count": N, ...}
```

### 4.2 Vector search
```python
from agents.core.knowledge.document_processor import search_documents
result = search_documents("your test query", n_results=3)
print(result)
# Expected: {"success": True, "count": N, "results": [...]}
```

### 4.3 Schema graph
Create or update any BI agent through the UI. The agent's schema graph
should build automatically. Check the log for:
```
NetworkXSchemaGraph: 'AgentName' built (N edges)
```
Verify edges were stored:
```sql
SELECT * FROM app_schema_graph WHERE agent_name = 'YourAgentName';
```

### 4.4 Schema embedding cache
Make one NLQ query through a BI agent. Then check:
```sql
SELECT COUNT(*) FROM app_schema_embedding_cache;
-- Should be > 0 after ~60 seconds (debounced flush)
```
Restart the service and make the same query — it should be noticeably faster
because embeddings are loaded from SQL on startup.

---

## 5. Azure Migration Playbook

When ready to migrate, work through these in order. Each step is independent
and can be done separately without breaking anything.

### Step 1 — Azure SQL Database (replaces SQL Server)

**Effort:** Low — connection string change only.

1. Create an Azure SQL Database (Basic or Standard tier is fine to start).
2. Run the app once locally pointing at Azure SQL — `ensure_tables()` will
   create all tables automatically.
3. Update `.env` (or your NSSM service environment):
   ```env
   DB_SERVER=yourserver.database.windows.net
   DB_PORT=1433
   DB_NAME=yourdbname
   DB_USER=sqladmin
   DB_PASS=yourpassword
   ```
4. The ODBC Driver 18 for SQL Server already supports Azure SQL.
   `TrustServerCertificate=yes` should be changed to `Encrypt=yes` for Azure.
   Update `app_db.py` → `get_app_db()` connection string:
   ```python
   f"Encrypt=yes;TrustServerCertificate=no;"
   ```

**Files changed:** `app_db.py` (connection string only, 1 line)

---

### Step 2 — Azure Blob Storage (replaces local file store)

**Effort:** Low — env var change + one new dependency.

1. Create an Azure Storage Account and a container (e.g. `agent-store`).
2. Install SDK: `pip install azure-storage-blob`
3. Set env vars:
   ```env
   AGENT_FILE_STORE=az://youraccountname/agent-store
   AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
   ```
4. Wrap file reads/writes in `registry.py` (`_FILE_DIR`, `_RAW_DIR`) with an
   `AzureBlobAdapter` that transparently maps local path operations to Azure
   Blob calls. The rest of the codebase uses path strings — only the I/O
   adapter changes.

**Files changed:** `registry.py`, `document_processor.py` (add blob adapter, ~30 lines)

---

### Step 3 — Azure AI Search (replaces SQL Server cosine search)

**Effort:** Very low — set two env vars, install one package. No code changes.

The `_AzureSearchVectorStore` backend is already fully implemented in
`vector_store.py`. It activates automatically when both env vars are present.

1. Create an Azure AI Search resource (Basic tier is enough to start;
   upgrade to Standard when chunk count exceeds ~50K).
2. Install SDK: `pip install azure-search-documents`
3. Add to `.env`:
   ```env
   AZURE_SEARCH_ENDPOINT=https://yourresource.search.windows.net
   AZURE_SEARCH_KEY=your-admin-key
   ```
4. Restart the app — it will automatically use Azure AI Search and create
   the `documents` and `knowledge` indexes on first startup.
5. Re-ingest your documents (the chunks in SQL Server are NOT migrated
   automatically — they're metadata only; re-run `process_document` or
   build a migration script that reads `app_document_chunks` and uploads).

Why upgrade: Azure AI Search uses HNSW indexing for nearest-neighbor search.
For large chunk counts (50K+), it's dramatically faster than the SQL Server
in-Python cosine loop. Below that threshold, SQL Server is fine.

**Files changed:** none — already implemented. Just env vars + pip install.

---

### Step 4 — Azure OpenAI (replaces OpenAI API)

**Effort:** Very low — endpoint + deployment name change.

1. Create Azure OpenAI resource, deploy `gpt-4o-mini` and `text-embedding-3-small`.
2. Update `.env`:
   ```env
   OPENAI_API_KEY=your-azure-openai-key
   OPENAI_API_BASE=https://yourresource.openai.azure.com/
   OPENAI_API_VERSION=2024-02-01
   OPENAI_API_TYPE=azure
   ```
3. In `nlq_engine.py`, update `ChatOpenAI` constructor:
   ```python
   self.llm = ChatOpenAI(
       azure_endpoint=os.getenv("OPENAI_API_BASE"),
       azure_deployment="gpt-4o-mini",
       api_version=os.getenv("OPENAI_API_VERSION"),
       ...
   )
   ```
4. For embeddings: replace `all-MiniLM-L6-v2` (local) with
   `text-embedding-3-small` via Azure OpenAI API, or keep the local model
   (it still works fine and avoids API costs for schema embeddings).

**Files changed:** `nlq_engine.py` (LLM constructor, ~5 lines),
optionally `vector_store.py` (embedding function)

---

### Step 5 — Azure Cosmos DB Gremlin (replaces NetworkX/SQL graph)  — Optional

**Effort:** Medium — only needed if you want a dedicated graph DB.

The NetworkX + Azure SQL approach works well indefinitely.
Only migrate if you need advanced graph analytics (multi-hop traversals,
graph algorithms at scale, visual graph exploration).

1. Create Azure Cosmos DB account with Gremlin API.
2. Install: `pip install gremlinpython`
3. In `schema_graph.py`, add a `CosmosGremlinSchemaGraph` class that mirrors
   the Neo4j `SchemaGraph` API (same methods). The existing Gremlin queries
   in `SchemaGraph` work against Cosmos DB Gremlin with minor dialect changes.
4. Update `get_schema_graph()` to return `CosmosGremlinSchemaGraph` when
   `COSMOS_GREMLIN_ENDPOINT` env var is set.

**Files changed:** `schema_graph.py` (~100 lines added, nothing removed)

---

### Migration Order Summary

```
Phase 1 (quick wins, no downtime):
  └── Azure SQL Database        ← 30 min, connection string change
  └── Azure Blob Storage        ← 1 hour, file I/O adapter

Phase 2 (vector search upgrade):
  └── Azure AI Search           ← 2-3 hours, VectorStore swap

Phase 3 (LLM):
  └── Azure OpenAI              ← 30 min, env vars + constructor

Phase 4 (optional, graph DB):
  └── Azure Cosmos DB Gremlin   ← 3-4 hours, only if needed
```

### What does NOT need to change
- All agent business logic
- All tool implementations (web_search, query_database, send_email, etc.)
- All workflow engine code
- All Flask routes and blueprints
- All frontend JS/HTML
- The ChromaDB → Azure AI Search swap is internal to `vector_store.py`
- The SQL Server → Azure SQL swap is internal to `app_db.py` / `config.py`
