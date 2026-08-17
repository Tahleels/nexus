# RAG System — Deep Dive

Complete technical reference for the Retrieval-Augmented Generation pipeline: ingestion (upload / filesystem watcher / SharePoint watcher), chunking, LanceDB vector storage, hybrid retrieval, confidence scoring, access control, and the HTTP/tool surface that exposes it.

Verified directly against code on 2026-06-29. Every file/line reference below was read, not inferred — if you change these files, this doc will drift, so re-verify before trusting it months from now.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Ingestion Entry Points](#ingestion-entry-points)
3. [Document Processor — `process_document()`](#document-processor)
4. [Deduplication](#deduplication)
5. [Extractors by File Type](#extractors-by-file-type)
6. [Chunking Strategy](#chunking-strategy)
7. [Key-Metrics Extraction (LLM)](#key-metrics-extraction)
8. [Vector Storage — `vector_store.py`](#vector-storage)
9. [Embedding Model](#embedding-model)
10. [Retrieval Pipeline — `rag_pipeline.py`](#retrieval-pipeline)
11. [Confidence Scoring](#confidence-scoring)
12. [Hallucination Guard](#hallucination-guard)
13. [Session Memory](#session-memory)
14. [Cache Layer](#cache-layer)
15. [Retrieval Traces & Stats](#retrieval-traces--stats)
16. [Knowledge Store (Key-Value)](#knowledge-store-key-value)
17. [Access Control (Document Visibility)](#access-control)
18. [Filesystem & SharePoint Watchers](#filesystem--sharepoint-watchers)
19. [Agent Tools](#agent-tools)
20. [HTTP API (`knowledge_bp.py`)](#http-api)
21. [Continuous Evaluation](#continuous-evaluation)
22. [Configuration Reference](#configuration-reference)
23. [Database Schema](#database-schema)
24. [Key Libraries](#key-libraries)

---

## Architecture Overview

```
                    ┌─────────────────────────────────────────────┐
                    │              INGESTION SOURCES               │
                    │  UI upload (knowledge_bp) │ process_document │
                    │  tool (chat attach) │ filesystem watcher │   │
                    │  SharePoint watcher (Graph API poll)         │
                    └───────────────────┬───────────────────────────┘
                                        ▼
                    document_processor.py :: process_document()
                      1. SHA-256 hash → dedup vs (hash, uploader, scope, scope_id)
                      2. Extractor dispatch by extension (12 formats)
                      3. _chunk_text() — 2000 chars, 200-char overlap
                      4. (optional) LLM key-metrics extraction → extra_meta
                      5. vector_store.add_chunks(doc_id, chunks)
                      6. _save_doc_meta() → app_documents (SQL Server)
                                        │
                                        ▼
                    vector_store.py :: get_vector_store()
                      Priority 1: Azure AI Search (if env vars set)
                      Priority 2: LanceDB (default, required dependency)
                        embed (all-MiniLM-L6-v2, 384-dim)
                        → queued to single writer thread
                        → Data/lancedb/document_chunks.lance
                        → IVF_PQ/HNSW cosine index once ≥256 rows
                      (SQL Server still holds metadata/cache/traces/KV store)

══════════════════════════════ query time ══════════════════════════════

  search_knowledge / search_connector_knowledge (agent tool, registry.py)
                                        │
                                        ▼
                    rag_pipeline.py :: RAGPipeline.run()
      0. get_visible_doc_ids(user_id, role) — access-control filter
      1. Cache check              → app_search_cache (MD5 key, 5 min TTL)
      2a. ANN vector search       → LanceDB HNSW, top 20 candidates
      2b. BM25 on those 20        → rank_bm25 (or TF fallback)
      2c. Hybrid merge            → 0.6·semantic + 0.4·BM25
      3.  Cross-encoder rerank    → ms-marco-MiniLM-L-6-v2 (soft-fail if absent)
      4.  Confidence scoring      → retrieval × trust × freshness × consistency
      5.  Hallucination guard     → fallback if max confidence < 0.20
      6.  Context block build     → "[Source N: file | Confidence: x.xx]" + citations
      7.  Session memory          → last-3-turn context prepended
      8.  Cache write + async trace → app_search_cache / app_retrieval_traces
                                        ▼
      Returns {context, citations, confidence, fallback, results, session_context}
                                        ▼
      Tool appends matching app_knowledge_store (KV) entries scoring ≥0.35
                                        ▼
      Agent/LLM receives context block, must answer only from it, cites [Source N]
```

---

## Ingestion Entry Points

All four converge on the same `process_document()` call:

| Entry point | File | Trigger |
|---|---|---|
| UI upload | [blueprints/knowledge_bp.py](../blueprints/knowledge_bp.py) `POST /api/knowledge/upload` | User uploads via Knowledge Base page |
| New version upload | `POST /api/knowledge/documents/<doc_id>/version` | Replacing an existing document |
| Chat attachment | `process_document` agent tool ([registry.py:1267](../agents/core/tools/registry.py#L1267)) | User attaches a file in chat with "Add to Knowledge Base" |
| Filesystem watcher | [services/document_watcher.py](../services/document_watcher.py) | File dropped into a configured watched directory |
| SharePoint watcher | [services/sharepoint_watcher.py](../services/sharepoint_watcher.py) | Polling a configured SharePoint folder via Microsoft Graph |

---

## Document Processor

**File:** `agents/core/knowledge/document_processor.py`, function `process_document()` (line 592).

```python
def process_document(file_path: str,
                     client_id:         str = "",
                     source_name:       str = "",
                     uploaded_by_id:    int = 0,
                     uploaded_by_name:  str = "",
                     version:           int = 1,
                     parent_doc_id:     Optional[str] = None,
                     scope:             str = "user",          # user|global|department|project
                     scope_id:          Optional[int] = None,  # dept_id / project_id
                     source_watch_id:   Optional[int] = None,  # connector ID
                     source_watch_type: str = "",              # filesystem|sharepoint
                     key_metrics:       Optional[List[str]] = None) -> Dict:
```

Steps, in order:

1. Resolve `file_path` (absolute, or filename relative to `Data/agent_store/raw_documents`).
2. SHA-256 hash the raw bytes (16-hex-char truncated) — dedup key.
3. Look up an existing `doc_id` for `(file_hash, uploaded_by_id, scope, scope_id)` — see [Deduplication](#deduplication).
4. Dispatch to the extractor for the file extension → `(text, extra_meta)`.
5. If no text extracted → return `{"success": False, "error": "No text could be extracted."}`.
6. `_chunk_text(text)` → list of overlapping chunks.
7. If `key_metrics` requested, run the LLM key-metrics extractor on the most relevant chunks → merged into `extra_meta["extracted_metrics"]`.
8. Build chunk dicts (text + metadata: document_id, filename, file_type, client_id, source_name, chunk_index, chunk_total).
9. `vector_store.get_vector_store().add_chunks(doc_id, chunks)` → embeds + persists, returns stored count.
10. `_save_doc_meta()` — MERGE upsert into `app_documents` (SQL Server) with chunk_count, status, scope, extra_meta.
11. Returns `{success, document_id, filename, file_type, chunk_count, text_length, vector_store_available, extraction_warning?}`.

---

## Deduplication

`_find_existing_doc_id(file_hash, uploaded_by_id, scope, scope_id)` ([document_processor.py:498](../agents/core/knowledge/document_processor.py#L498)) matches on **content hash + owner + scope** — not content hash alone:

- Same file, same uploader, same scope → reuses the existing `doc_id` (re-ingestion refreshes in place instead of duplicating).
- Same bytes uploaded by a different user, or under a different scope, by the same user → always a brand-new document. Visibility is never accidentally merged across owners.
- Covers system/connector uploads (`uploaded_by_id=0`): re-scanning the same department/global-scoped file by a watcher updates it in place rather than creating duplicates on every scan cycle.

---

## Extractors by File Type

`_EXTRACTOR_MAP` in `document_processor.py` (line 371):

| Extension(s) | Library | Notes |
|---|---|---|
| `.pdf` | `pdfplumber` | Per-page `extract_text()`, joined with `\n\n`; records `page_count` |
| `.docx` | `python-docx` | Paragraph text + table cells (`\|`-joined) |
| `.doc` (legacy binary) | `win32com` → LibreOffice headless → `docx2txt` | 3-stage fallback chain — see below |
| `.xlsx`, `.xls`, `.xlsm` | `pandas` | First 10 sheets, up to 500 rows each, stringified |
| `.csv` | `pandas` | Tries `utf-8-sig` → `utf-8` → `latin-1` → `cp1252` encodings in order; first 1000 rows |
| `.pptx`, `.ppt` | `python-pptx` | All shape text per slide, labeled `Slide N:` |
| `.html`, `.htm` | `beautifulsoup4` | Strips `script/style/nav/footer/header`; collapses blank lines |
| `.eml` | stdlib `email` | Subject/From/Date + first `text/plain` part |
| `.msg` | `extract-msg` | Outlook message extraction |
| `.png`, `.jpg`, `.jpeg`, `.tiff`, `.bmp` | `easyocr` | CPU-only (`gpu=False`); optional dependency, soft-fails if missing |
| `.txt`, `.md` | stdlib | Direct read, `errors="replace"` |
| `.json` | stdlib | Re-serialized with `indent=2` for readability |

### `.doc` legacy fallback chain

`_extract_doc_legacy()` ([document_processor.py:160](../agents/core/knowledge/document_processor.py#L160)):
1. **win32com** — drives a real (hidden) Word instance via COM automation. Only works if Word is installed (rare on a server).
2. **LibreOffice headless** — `soffice --headless --convert-to docx`, then parses the resulting `.docx` normally. Looked up via `PATH` or two hardcoded Program Files locations.
3. **docx2txt** — last resort, handles `.doc` files that are actually OOXML mislabeled with the old extension.

---

## Chunking Strategy

```python
_CHUNK_CHARS   = 2000   # ~500 tokens at 4 chars/token
_OVERLAP_CHARS = 200    # ~50-token overlap between consecutive chunks
```

`_chunk_text()` ([document_processor.py:64](../agents/core/knowledge/document_processor.py#L64)): if the full text fits in one chunk, returns it as-is. Otherwise walks forward in `_CHUNK_CHARS`-sized windows, but for each window searches backward (within the overlap region) for the best break point — paragraph break (`\n\n`) preferred, then sentence end (`. `), then word boundary (` `) — so chunks don't split mid-sentence where avoidable. Each new chunk starts `_OVERLAP_CHARS` before the previous chunk's end, so context that would otherwise be lost at a boundary appears in both chunks.

---

## Key-Metrics Extraction

Optional step, only runs when the caller passes `key_metrics` (a list of metric names to extract — e.g. `["total revenue", "contract end date"]`).

`_select_metric_chunks()` ([document_processor.py:411](../agents/core/knowledge/document_processor.py#L411)) scores every chunk by how many of the requested keywords it contains, then greedily packs the highest-scoring chunks into a 4000-char budget (falls back to the first two chunks if nothing scores). `_extract_key_metrics()` sends only that small slice to `gpt-4o-mini` (via `OPENAI_API_KEY`, raw `urllib` call, `response_format: json_object`, `temperature=0`) asking for a JSON object mapping metric name → value or `null`. Result merges into the document's `extra_meta["extracted_metrics"]`. Fails silently (returns `{}`) if no API key, no chunks, or the call errors.

---

## Vector Storage

**File:** `agents/core/knowledge/vector_store.py`

### Backend selection — `get_vector_store()` (line 953)

```
1. Azure AI Search   — if AZURE_SEARCH_ENDPOINT + AZURE_SEARCH_KEY are set and init succeeds
2. LanceDB           — default; raises RuntimeError if `lancedb` isn't installed
                        (it is a REQUIRED dependency, not an optional fallback)
```

SQL Server is **not** a vector-backend fallback anymore. It still backs: document metadata (`app_documents`), access control (`app_document_assignments`), the small KV knowledge store (`app_knowledge_store`), the query cache (`app_search_cache`), and retrieval traces (`app_retrieval_traces`).

### LanceDB backend — `_LanceDBVectorStore` (line 596)

- **Storage:** `Data/lancedb/document_chunks.lance` (path overridable via `LANCEDB_PATH`). Table `document_chunks`, PyArrow schema: `id, document_id, chunk_index, text_content, vector(384×float32), filename, file_type, client_id, source_name`.
- **Single writer thread:** every write (`add_chunks`, `delete_document`, `migrate_from_sql`) is queued as a `_WriteJob` to one dedicated background thread (`_writer_loop`). The calling thread blocks on `job.done.wait()` until it completes. This serializes all LanceDB mutations — LanceDB itself isn't safe for concurrent writers from multiple threads/processes, so this is the concurrency guard.
- **Indexing:** `_maybe_index()` builds a cosine `IVF_PQ`/HNSW index once the table reaches **256 rows** (`_MIN_INDEX_ROWS`). Below that threshold, LanceDB does brute-force search (same accuracy; index creation would actually error out on too few rows anyway). Re-indexes (`replace=True`) after every `add_chunks` call once past the threshold.
- **Auto-migration:** the first time `get_vector_store()` is called and the LanceDB table is empty but `app_document_chunks` (SQL) has rows, `migrate_from_sql()` copies them over in batches of 500, skipping any `document_id` already present in LanceDB. Safe to call repeatedly.
- **Search:** `search_documents()` builds a `.search(q_emb).metric("cosine").limit(n)` query; if `doc_ids` is given, applies a `prefilter=True` SQL-style `WHERE document_id IN (...)` so target documents are always reachable regardless of corpus size (avoids the "true match got pushed out of the ANN top-N by unrelated documents" problem). Returns cosine similarity as `1 - distance`.
- **Knowledge store (KV):** delegated entirely to an internal `_SQLVectorStore` instance — too small a dataset to be worth a second storage system.

### Azure AI Search backend — `_AzureSearchVectorStore` (line 342)

Activated by setting both `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_KEY`. On init, creates two indexes (`documents`, `knowledge`) with an HNSW `VectorSearchProfile` if they don't already exist. Exposes the exact same method surface as the LanceDB/SQL backends (`add_chunks`, `search_documents`, `delete_document`, `set_knowledge`, `get_knowledge`, `search_knowledge`, `stats`) — nothing outside `vector_store.py` needs to change to migrate.

### SQL Server backend — `_SQLVectorStore` (line 98)

Legacy/fallback backend: stores embeddings as JSON strings in `app_document_chunks.embedding`, ranks via `_cosine_top_k()` (numpy + `sklearn.metrics.pairwise.cosine_similarity`) — a full in-Python loop over every row matching the filter, no ANN index. Still used directly for the KV knowledge store (both standalone and via LanceDB's delegation) since that dataset is small enough that brute-force cosine is fine.

---

## Embedding Model

**Model:** `sentence-transformers/all-MiniLM-L6-v2` — 384 dimensions, CPU-only, lazy-loaded on first use (`_get_model()` in `vector_store.py`). Same model for both indexing and querying (required for cosine similarity to be meaningful). Batch-encoded (`batch_size=32`) during ingestion for efficiency.

---

## Retrieval Pipeline

**File:** `agents/core/knowledge/rag_pipeline.py`, class `RAGPipeline`, entry point `run()` (line 367).

```python
def run(self, query: str,
        doc_ids: Optional[Set[str]] = None,        # access-control filter
        pinned_doc_ids: Optional[Set[str]] = None,  # user-attached docs, bypass guard
        n_results: int = 8,
        user_id: int = 0,
        session_id: str = "") -> Dict:
```

**Step 1 — normalize + cache check.** Collapses whitespace in the query, builds `md5(query|user_id|sorted(doc_ids))`, checks `app_search_cache` (5 min TTL via `RAG_CACHE_TTL`).

**Step 2a — ANN vector search.** `vector_store.search_documents(query, n_results=20, doc_ids=doc_ids)` — LanceDB HNSW cosine search, `_RERANK_TOP_K = 20` candidates. If nothing comes back, short-circuits to a fallback/empty response.

**Step 2b — BM25 on those 20 candidates only** (not the full corpus — keeps it cheap). `rank_bm25.BM25Okapi` if installed; otherwise a raw token-hit-count fallback normalized to `[0,1]`.

**Step 2c — hybrid merge.** `score = α·vector_score + (1-α)·bm25_score`, `α = 0.6` (`RAG_HYBRID_ALPHA`). Re-sorts the 20 candidates by this blended score.

**Step 3 — cross-encoder rerank** (optional, `use_reranker=True` by default). `cross-encoder/ms-marco-MiniLM-L-6-v2` jointly scores `(query, chunk_text)` pairs and re-sorts. Lazily loaded once per process; if `sentence-transformers` import fails, logs once and silently skips for the rest of the process lifetime (`_reranker_tried` flag prevents retrying every call).

**Step 4 — enrichment.** Batch-fetches `created_at` from `app_documents` for just the doc IDs in the final top-N (not the full 20) — a small targeted SQL lookup.

**Step 5 — confidence scoring.** See [Confidence Scoring](#confidence-scoring) below.

**Step 6 — hallucination guard.** See [Hallucination Guard](#hallucination-guard) below.

**Step 7 — context block build.** See `_build_context()` — formats up to `RAG_MAX_CONTEXT_CHARS` (12,000) characters as `[Source N: filename | Confidence: x.xx]\n<text>` blocks separated by `---`, plus a parallel `citations` list (`[N] filename | chunk K | YYYY-MM-DD`).

**Step 8 — session memory.** Prepends prior-turn context (see [Session Memory](#session-memory)) and records this turn for the next one.

**Step 9 — cache write + async trace.** Writes the full output to `app_search_cache`, then best-effort inserts a row into `app_retrieval_traces` (wrapped in try/except — a tracing failure never fails the request).

Returns:
```python
{
    "context": str, "citations": [str], "confidence": float, "consistency": float,
    "fallback": bool, "results": [...], "session_context": str,
    "from_cache": bool, "latency_ms": int,
}
```

---

## Confidence Scoring

```
confidence = retrieval_score × file_trust × freshness × consistency_weight
```

| Factor | Source | Range |
|---|---|---|
| `retrieval_score` | hybrid (or rerank) score from steps 2c/3 | 0–1 |
| `file_trust` | `_FILE_TRUST` lookup by extension | 0.40 (image) – 0.90 (pdf/docx) |
| `freshness` | `exp(-age_days / 180)` | ~0–1, half-life ≈180 days |
| `consistency_weight` | `0.7 + 0.3 × min(1, distinct_docs/3)` | 0.80 (1 doc) – 1.00 (3+ docs) |

`_FILE_TRUST` table (`rag_pipeline.py:57`): pdf/docx 0.90, doc 0.85, xlsx/xls/xlsm 0.80, pptx 0.75, csv/json 0.70, ppt 0.70, txt/md 0.65, eml/msg 0.60, html/htm 0.55, images 0.40. Unknown types default to 0.60.

`_consistency_score()` rewards multiple distinct source documents agreeing on an answer — a single source still gets 0.33 (not 0) "so it still has a fighting chance," per the code comment.

---

## Hallucination Guard

```python
_CONFIDENCE_THRESHOLD = 0.20   # RAG_CONFIDENCE_THRESHOLD
max_confidence = max(chunk confidences, default=0)
fallback = max_confidence < threshold
```

**Exception:** if the caller passed `pinned_doc_ids` (documents the user explicitly attached/pinned to the conversation) and any top chunk belongs to one, `fallback` is forced `False` — a user-attached document is always trusted regardless of score.

When `fallback=True`, `_build_context()` returns a fixed instruction block telling the LLM there's insufficient evidence and it must say so rather than guess, with zero citations.

---

## Session Memory

Two-layer cache, keyed by `session_id`:
- **L1** — in-process Python dict (`_session_store`), zero-latency within the same process.
- **L2** — `app_rag_sessions` SQL table (`MERGE` upsert), survives process restarts.

Stores the last `_SESSION_MEMORY_LEN = 3` turns as `{query, context[:500]}`. On the next call, `_session_context()` formats prior turns as a `"=== PRIOR CONVERSATION CONTEXT ==="` block and prepends it ahead of the current retrieval — enabling follow-ups like "and what about last year?" to resolve against what was previously retrieved.

---

## Cache Layer

`app_search_cache`, keyed by `md5(query|user_id|doc_ids_repr)` — note the doc-ids filter is part of the key, so two users with different visibility scopes never share a cached answer. TTL defaults to 300s (`RAG_CACHE_TTL`). Read checks `expires_at > GETUTCDATE()` and bumps `hit_count`; write is a `MERGE` upsert. Both read and write are wrapped in bare `except: pass` — a cache outage degrades to "always recompute," never breaks retrieval.

---

## Retrieval Traces & Stats

Every `run()` call (success or fallback) best-effort inserts into `app_retrieval_traces`: session_id, user_id, query, doc_ids (JSON), top 5 scores (JSON), method (`"hybrid_ann"`), result_count, confidence, fallback flag, latency_ms.

- `get_rag_stats(days=7)` — aggregate query count, avg latency/confidence, fallback rate, top-5 most-searched queries, and latency buckets (`<500ms` / `500-2000ms` / `>2000ms`). Surfaced at `GET /api/knowledge/rag-stats` and the `/rag-dashboard` page.
- `GET /api/knowledge/rag-traces` — raw trace rows.

---

## Knowledge Store (Key-Value)

A separate, much smaller store (`app_knowledge_store`) for agent-managed facts (not document chunks) — set/get/delete/list via the `manage_knowledge` tool, each value embedded for semantic lookup. `search_knowledge` (the RAG tool) additionally runs `vector_store.search_knowledge(query, n_results=3)` and appends any entries scoring ≥0.35 after the document context, under a `=== KNOWLEDGE BASE ENTRIES ===` header — so agents see both document-derived and manually-curated facts in one context block.

---

## Access Control

`get_visible_doc_ids(user_id, role)` ([document_processor.py:764](../agents/core/knowledge/document_processor.py#L764)):

| Role | Result |
|---|---|
| `admin`, `dev` | `None` — no filter, sees everything |
| anyone else | the **union** of: `scope='global'` docs, the user's own `scope='user'` uploads, docs in `app_document_assignments` for this user, docs in any `scope='department'`/`scope='project'` the user belongs to (via `user_departments`/`user_projects`) |

This filter is applied as a SQL-level `doc_ids` set in `vector_store.search_documents()`, not a post-filter — so a restricted user's search always reaches their visible documents regardless of how many other documents exist in the table.

---

## Filesystem & SharePoint Watchers

### `services/document_watcher.py`

Watches configured directories (`document_watch_dirs` table) via `watchdog` (polling fallback if unavailable). On a new/settled file (3s settle delay to avoid partial writes):
1. Skip if extension unsupported, or path is inside `venv/__pycache__/.git/node_modules/archive/...` (`_SKIP_DIRS`).
2. `process_document()` with the watch's configured `scope`/`scope_id`. For `scope='user'` watches with a single `target_user_id` and no explicit `user_ids`, the document is attributed directly to that user (so `get_visible_doc_ids` restricts it to them).
3. Explicit `user_ids` list (if configured) → `assign_document()` grants for each.
4. Move the source file to `<watched-dir>/archive/` (renamed with a timestamp suffix on collision) — archived files are never re-ingested.
5. Log the outcome to `connector_logs` and bump `document_watch_dirs.files_ingested`.

### `services/sharepoint_watcher.py`

Per configured watch (`sharepoint_watch_configs`), polls a SharePoint folder via Microsoft Graph at a configurable interval (default 60s):
1. OAuth2 client-credentials flow (`SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET`) — token cached process-wide, refreshed ~60s before expiry.
2. Skip the `archive` subfolder and anything modified within the last 10s (`_SETTLE_SECS`).
3. Download and run through the same `process_document()` path.
4. Move the processed file to `archive/` **within SharePoint** itself (not local disk).
5. Update DB counters, same as the filesystem watcher.

Both watchers feed `search_connector_knowledge`, which scopes retrieval to `app_documents` rows matching a given `source_watch_id` + `source_watch_type`.

---

## Agent Tools

Registered in `agents/core/tools/registry.py` (`TOOLS` dict, line ~2196):

| Tool | Schema | Notes |
|---|---|---|
| `process_document` | `file_path` (required), `source_name` | Ingests an attached chat file |
| `search_knowledge` | `query` (required), `n_results`, `document_ids` | Full hybrid pipeline, access-control filtered; `document_ids` can pin specific docs (bypasses hallucination guard for those) |
| `search_connector_knowledge` | `query` (required), `n_results`, `connector_keys` | Scoped to one or more `filesystem:<id>` / `sharepoint:<id>` connectors |
| `manage_knowledge` | `action` (get/set/delete/list), `key`, `value` | KV knowledge store, not document chunks |

`search_knowledge`'s implementation ([registry.py:1288](../agents/core/tools/registry.py#L1288)) resolves the caller's `user_id`/`role` from `_hub_ctx`, intersects any explicitly-configured `document_ids` with what the user can actually see (unless admin/dev), then calls `RAGPipeline.run()`.

---

## HTTP API

`blueprints/knowledge_bp.py` — selected routes (full list has 35+ endpoints covering org/assignment management):

| Route | Purpose |
|---|---|
| `POST /api/knowledge/upload` | Upload + ingest a new document (scope, scope_id, optional explicit user assignment) |
| `POST /api/knowledge/documents/<id>/version` | Upload a replacement version of an existing document |
| `GET /api/knowledge/documents` | List visible documents (filterable by scope) |
| `DELETE /api/knowledge/documents/<id>` | Remove a document (chunks + metadata + assignments + raw file + agent tool-config references) |
| `POST /api/knowledge/documents/<id>/assign` | Grant a specific user visibility |
| `POST /api/knowledge/search` | Ad-hoc RAG search from the UI |
| `GET/POST/PUT/DELETE /api/knowledge/watch-dirs` | Manage filesystem watch connectors |
| `POST /api/knowledge/watch-dirs/<id>/scan` | Force an immediate directory scan |
| `GET/POST/PUT/DELETE /api/knowledge/sharepoint-watches` | Manage SharePoint watch connectors |
| `GET /api/knowledge/rag-traces`, `/rag-stats`, `/rag-dashboard` | Observability |
| `GET/POST/DELETE /api/knowledge/eval-queries`, `POST /eval-run`, `GET /eval-results` | Evaluation harness |
| `GET /api/knowledge/connector-logs` | Per-file ingestion success/error log |

---

## Continuous Evaluation

`rag_pipeline.py` ships a self-contained recall benchmark:

```python
add_eval_query(query="What is the refund policy?", expected_doc_ids=["doc_abc123"], category="recall")
run_evaluation(user_id=0)
# → {total_queries, recall_at_1, recall_at_3, recall_at_5, fallback_rate, avg_latency_ms}
```

`run_evaluation()` runs every registered query through a fresh `RAGPipeline(use_cache=False)`, checks whether any `expected_doc_ids` appear in the top-1/3/5 results, and persists each run to `app_rag_eval_results` for trend tracking over time. Surfaced at `/api/knowledge/eval-*` routes.

---

## Configuration Reference

| Variable | Default | Effect |
|---|---|---|
| `RAG_HYBRID_ALPHA` | `0.6` | Semantic-vs-BM25 weight in step 2c (1.0 = pure vector, 0.0 = pure keyword) |
| `RAG_CONFIDENCE_THRESHOLD` | `0.20` | Below this max confidence → hallucination-guard fallback |
| `RAG_CACHE_TTL` | `300` (sec) | Query result cache lifetime |
| `RAG_MAX_CONTEXT_CHARS` | `12000` | Hard cap on context block size sent to the LLM |
| `LANCEDB_PATH` | `Data/lancedb` | Override LanceDB storage directory |
| `AZURE_SEARCH_ENDPOINT` / `AZURE_SEARCH_KEY` | — | Set both to switch the vector backend to Azure AI Search |
| `AGENT_FILE_STORE` | `Data/agent_store` | Override raw uploaded-file storage directory |
| `SP_TENANT_ID` / `SP_CLIENT_ID` / `SP_CLIENT_SECRET` | — | SharePoint watcher Azure AD app credentials |
| `OPENAI_API_KEY` | — | Required for optional key-metrics LLM extraction at ingest time |

---

## Database Schema

All in SQL Server (`nexus` DB), defined in `database/app_db.py`:

| Table | Key columns |
|---|---|
| `app_documents` | id (PK), filename, file_type, client_id, source_name, chunk_count, file_hash, extra_meta (JSON), uploaded_by_id/name, version, parent_doc_id, status, **scope, scope_id**, source_watch_id, source_watch_type, created_at |
| `app_document_chunks` | id (PK, `<doc_id>_<i>`), document_id, chunk_index, text_content, embedding (JSON; legacy/KV path only — LanceDB stores vectors natively), filename, file_type, client_id, source_name |
| `app_document_assignments` | doc_id, user_id, user_name, assigned_by_id/name, assigned_at — unique per (doc_id, user_id) |
| `app_knowledge_store` | key_name (unique), value, embedding (JSON), updated_at |
| `app_search_cache` | cache_key (PK, MD5), query, user_id, results (JSON), hit_count, expires_at |
| `app_rag_sessions` | session_id (PK), user_id, turns_json, updated_at |
| `app_retrieval_traces` | session_id, user_id, query, doc_ids (JSON), top_scores (JSON), method, result_count, confidence, fallback, latency_ms, created_at |
| `app_rag_eval_queries` | query, expected_doc_ids (JSON), category, created_at |
| `app_rag_eval_results` | eval_query_id (FK), recall_at_1/3/5, confidence, consistency, fallback, latency_ms, actual_doc_ids (JSON), run_at |
| `document_watch_dirs` | folder_path, label, scope, scope_id, target_user_id, created_by, last_scanned, enabled, files_ingested |
| `sharepoint_watch_configs` | site_url, sp_folder_path, label, scope, scope_id, target_user_id, poll_interval, cached_site_id/drive_id, files_ingested |
| `connector_logs` | watch_type, watch_id, watch_label, filename, status, chunk_count, error_msg, created_at |

**Outside LanceDB:** vectors live in `Data/lancedb/document_chunks.lance` (PyArrow table `document_chunks`), not in `app_document_chunks.embedding` — that SQL column is now legacy/migration-source data plus the live store for the small KV knowledge-store path.

---

## Key Libraries

| Library | Install | Used for |
|---|---|---|
| `lancedb` | `pip install lancedb` | **Required.** Primary vector store + ANN index |
| `pyarrow` | (lancedb dependency) | LanceDB table schema |
| `sentence-transformers` | `pip install sentence-transformers` | Embedding model + cross-encoder reranker |
| `rank-bm25` | `pip install rank-bm25` | BM25 keyword scoring (optional — TF fallback otherwise) |
| `numpy`, `scikit-learn` | — | Cosine similarity for the legacy SQL backend / KV store |
| `pdfplumber` | `pip install pdfplumber` | PDF extraction |
| `python-docx` | `pip install python-docx` | DOCX extraction |
| `pywin32` (`win32com`) | `pip install pywin32` | Legacy `.doc` extraction via Word COM (optional) |
| `docx2txt` | `pip install docx2txt` | Last-resort `.doc` extraction |
| `pandas`, `openpyxl` | — | XLSX/XLS/CSV extraction |
| `python-pptx` | `pip install python-pptx` | PPT/PPTX extraction |
| `beautifulsoup4` | `pip install beautifulsoup4` | HTML extraction |
| `extract-msg` | `pip install extract-msg` | Outlook `.msg` extraction |
| `easyocr` | `pip install easyocr` | Image OCR (optional, CPU-only) |
| `watchdog` | `pip install watchdog` | Filesystem watcher (optional — polling fallback) |
| `requests` | — | SharePoint Graph API calls |
| `azure-search-documents` | `pip install azure-search-documents` | Azure AI Search backend (optional) |
