"""
vector_store.py — SQL Server-backed vector store with Azure AI Search migration path.

Storage layout (no SQLite files, no extra infrastructure)
----------------------------------------------------------
  app_document_chunks   — text chunks + embeddings from ingested documents
  app_knowledge_store   — key/value knowledge store (embedding column added)

Both tables live in your existing SQL Server database.
They migrate to Azure SQL Database with zero code changes.

Azure AI Search migration
-------------------------
Set two env vars and the VectorStore automatically switches backends:
  AZURE_SEARCH_ENDPOINT=https://yourresource.search.windows.net
  AZURE_SEARCH_KEY=your-admin-key

The rest of the codebase calls get_vector_store() and never sees the switch.

Embeddings
----------
Uses sentence-transformers all-MiniLM-L6-v2 (384 dims, already in requirements).
Similarity is cosine, computed in Python with numpy/sklearn.
For very large chunk counts (100K+), Azure AI Search's HNSW index replaces
the in-Python cosine loop — that's the migration gain, not just hosting.
"""
import json
from logging_config import get_logger
import os
import queue
import sys
import threading
from typing import Dict, List, Optional

logger = get_logger(__name__)

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Embedding helper ──────────────────────────────────────────────────────────
_emb_model = None


def _get_model():
    """Lazily load and cache the process-wide SentenceTransformer (all-MiniLM-L6-v2) model."""
    global _emb_model
    if _emb_model is None:
        from sentence_transformers import SentenceTransformer
        _emb_model = SentenceTransformer('all-MiniLM-L6-v2')
    return _emb_model


def _embed(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts. Returns list of float lists."""
    model = _get_model()
    return model.encode(texts, batch_size=32, show_progress_bar=False).tolist()


def _embed_one(text: str) -> List[float]:
    """Embed a single text string. Returns its 384-dim float vector."""
    return _embed([text])[0]


def _cosine_top_k(query_emb: List[float], rows: List[Dict],
                  emb_key: str, n: int) -> List[Dict]:
    """
    Rank rows by cosine similarity to query_emb, return top-n.

    Used by the SQL-backed brute-force search paths (document chunks and
    the knowledge store) where embeddings are stored as JSON strings/lists
    under `emb_key` on each row.

    Args:
        query_emb: Query embedding vector.
        rows: Candidate rows, each with an embedding under `emb_key`
            (JSON string or list); rows with no/invalid embedding are
            skipped.
        emb_key: Dict key holding the row's embedding.
        n: Number of top-scoring rows to return.

    Returns:
        Up to `n` row dicts (copies), sorted by descending similarity, each
        with an added "_score" float field. Empty list if no rows have a
        usable embedding.
    """
    import numpy as np
    from sklearn.metrics.pairwise import cosine_similarity
    if not rows:
        return []
    q = np.array(query_emb, dtype='float32').reshape(1, -1)
    vecs, valid = [], []
    for row in rows:
        raw = row.get(emb_key)
        if raw:
            try:
                vecs.append(json.loads(raw) if isinstance(raw, str) else raw)
                valid.append(row)
            except Exception:
                pass
    if not vecs:
        return []
    mat    = np.array(vecs, dtype='float32')
    scores = cosine_similarity(q, mat)[0]
    order  = scores.argsort()[::-1][:n]
    result = []
    for i in order:
        row = dict(valid[i])
        row['_score'] = round(float(scores[i]), 4)
        result.append(row)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SQL SERVER BACKEND  (default, no extra infrastructure)
# ═══════════════════════════════════════════════════════════════════════════════

class _SQLVectorStore:
    """
    Stores embeddings in SQL Server.
    Works identically against Azure SQL Database — no code changes needed.
    """

    @property
    def available(self) -> bool:
        """Always True — SQL Server is always considered available."""
        return True

    # ── Document chunks ───────────────────────────────────────────────────────

    def add_chunks(self, document_id: str, chunks: List[Dict]) -> int:
        """
        Persist chunks for a document.
        Each chunk: {"text": str, "metadata": dict}
        Returns the number of chunks stored.
        """
        if not chunks:
            return 0
        texts = [c["text"] for c in chunks]
        try:
            embeddings = _embed(texts)
        except Exception as exc:
            logger.warning("VectorStore.add_chunks: embedding failed: %s", exc)
            embeddings = [[] for _ in texts]

        from app_db import get_app_db
        stored = 0
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                    meta      = chunk.get("metadata", {})
                    chunk_id  = f"{document_id}_{i}"
                    cur.execute("""
                        MERGE app_document_chunks AS t
                        USING (SELECT ? AS id) AS s ON t.id = s.id
                        WHEN NOT MATCHED THEN INSERT
                            (id, document_id, chunk_index, text_content, embedding,
                             filename, file_type, client_id, source_name)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, chunk_id,
                        chunk_id, document_id, i, chunk["text"],
                        json.dumps(emb),
                        meta.get("filename", ""),
                        meta.get("file_type", ""),
                        meta.get("client_id", ""),
                        meta.get("source_name", ""))
                    stored += 1
                conn.commit()
        except Exception as exc:
            logger.warning("VectorStore.add_chunks: SQL failed: %s", exc)
        return stored

    def search_documents(self, query: str, n_results: int = 5,
                         where: Optional[Dict] = None,
                         doc_ids: Optional[set] = None) -> List[Dict]:
        """
        Semantic search over document chunks.

        doc_ids  : when provided, only chunks from these document IDs are
                   loaded from SQL before cosine ranking.  This guarantees
                   the target documents are always searched regardless of
                   how many other documents exist in the table.
        """
        try:
            q_emb = _embed_one(query)
        except Exception as exc:
            logger.warning("VectorStore.search_documents: embed failed: %s", exc)
            return []

        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                base_cols = """
                    SELECT id, document_id, chunk_index, text_content, embedding,
                           filename, file_type, client_id, source_name
                    FROM app_document_chunks
                """
                if doc_ids:
                    # Filter at SQL level — avoids the top-N cosine exclusion problem
                    ph = ",".join(["?" for _ in doc_ids])
                    cur.execute(f"{base_cols} WHERE document_id IN ({ph})", *list(doc_ids))
                elif where and where.get("client_id"):
                    cur.execute(f"{base_cols} WHERE client_id = ?", where["client_id"])
                else:
                    cur.execute(base_cols)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            logger.warning("VectorStore.search_documents: SQL fetch failed: %s", exc)
            return []

        ranked = _cosine_top_k(q_emb, rows, "embedding", n_results)
        return [
            {
                "text":        r["text_content"],
                "document_id": r["document_id"],
                "filename":    r["filename"],
                "source_name": r["source_name"],
                "score":       r["_score"],
                "metadata": {
                    "file_type":   r["file_type"],
                    "client_id":   r["client_id"],
                    "chunk_index": r["chunk_index"],
                },
            }
            for r in ranked
        ]

    def delete_document(self, document_id: str) -> bool:
        """Delete all chunks for a document from `app_document_chunks`. Returns True on success."""
        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM app_document_chunks WHERE document_id = ?",
                    document_id)
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("VectorStore.delete_document: %s", exc)
            return False

    def document_chunk_count(self) -> int:
        """Return the total row count in `app_document_chunks` (0 on error)."""
        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM app_document_chunks")
                return cur.fetchone()[0]
        except Exception:
            return 0

    # ── Knowledge store ───────────────────────────────────────────────────────

    def set_knowledge(self, key: str, value: str,
                      metadata: Optional[Dict] = None) -> bool:
        """
        Upsert a key/value entry in the knowledge store, embedding `value`
        for later semantic search.

        Args:
            key: Unique knowledge-store key.
            value: Text value to store and embed.
            metadata: Unused by this backend (kept for interface parity).

        Returns:
            True on success, False on embedding or SQL failure.
        """
        try:
            emb = _embed_one(value)
        except Exception:
            emb = []
        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    MERGE app_knowledge_store AS t
                    USING (SELECT ? AS key_name) AS s ON t.key_name = s.key_name
                    WHEN MATCHED THEN
                        UPDATE SET value = ?, embedding = ?, updated_at = GETUTCDATE()
                    WHEN NOT MATCHED THEN
                        INSERT (key_name, value, embedding)
                        VALUES (?, ?, ?);
                """, key, value, json.dumps(emb), key, value, json.dumps(emb))
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("VectorStore.set_knowledge: %s", exc)
            return False

    def get_knowledge(self, key: str) -> Optional[str]:
        """Return the stored value for `key`, or None if not found/on error."""
        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT value FROM app_knowledge_store WHERE key_name = ?", key)
                row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def search_knowledge(self, query: str, n_results: int = 5) -> List[Dict]:
        """Semantic search over the knowledge store."""
        try:
            q_emb = _embed_one(query)
        except Exception as exc:
            logger.warning("VectorStore.search_knowledge: embed failed: %s", exc)
            return []

        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT key_name, value, embedding
                    FROM app_knowledge_store
                    WHERE embedding IS NOT NULL
                """)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            logger.warning("VectorStore.search_knowledge: SQL failed: %s", exc)
            return []

        ranked = _cosine_top_k(q_emb, rows, "embedding", n_results)
        return [
            {
                "key":      r["key_name"],
                "value":    r["value"],
                "score":    r["_score"],
                "metadata": {},
            }
            for r in ranked
        ]

    def delete_knowledge(self, key: str) -> bool:
        """Delete a knowledge-store entry by key. Returns True on success."""
        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM app_knowledge_store WHERE key_name = ?", key)
                conn.commit()
            return True
        except Exception:
            return False

    def list_knowledge_keys(self) -> List[str]:
        """Return all knowledge-store keys, alphabetically sorted (empty list on error)."""
        from app_db import get_app_db
        try:
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT key_name FROM app_knowledge_store ORDER BY key_name")
                return [r[0] for r in cur.fetchall()]
        except Exception:
            return []

    def stats(self) -> Dict:
        """Return backend identity + availability + chunk/knowledge counts."""
        return {
            "backend":          "sql_server",
            "available":        True,
            "document_chunks":  self.document_chunk_count(),
            "knowledge_entries": len(self.list_knowledge_keys()),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# AZURE AI SEARCH BACKEND  (activated by setting AZURE_SEARCH_ENDPOINT env var)
# ═══════════════════════════════════════════════════════════════════════════════

class _AzureSearchVectorStore:
    """
    Azure AI Search backend.
    Install: pip install azure-search-documents
    Set env vars:
        AZURE_SEARCH_ENDPOINT=https://yourresource.search.windows.net
        AZURE_SEARCH_KEY=your-admin-key
    Two indexes are created automatically: 'documents' and 'knowledge'.

    This class exposes the exact same interface as _SQLVectorStore so
    nothing outside vector_store.py needs to change on migration.
    """

    _VECTOR_DIM = 384  # all-MiniLM-L6-v2

    def __init__(self):
        """Read Azure Search credentials from env vars and attempt to connect/init indexes."""
        self._endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
        self._key      = os.environ.get("AZURE_SEARCH_KEY", "")
        self._docs_client  = None
        self._know_client  = None
        self._available    = False
        self._init()

    def _init(self):
        """
        Connect to Azure AI Search and ensure the 'documents'/'knowledge'
        indexes exist (creating them with an HNSW vector profile if not).
        Sets `self._available` True only on full success; logs and leaves
        it False if azure-search-documents isn't installed or init fails.
        """
        try:
            from azure.search.documents import SearchClient
            from azure.search.documents.indexes import SearchIndexClient
            from azure.search.documents.indexes.models import (
                SearchIndex, SimpleField, SearchableField,
                SearchField, SearchFieldDataType, VectorSearch,
                HnswAlgorithmConfiguration, VectorSearchProfile,
            )
            from azure.core.credentials import AzureKeyCredential

            cred        = AzureKeyCredential(self._key)
            idx_client  = SearchIndexClient(self._endpoint, cred)

            vector_search = VectorSearch(
                algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
                profiles=[VectorSearchProfile(name="vp", algorithm_configuration_name="hnsw")],
            )

            for index_name in ("documents", "knowledge"):
                fields = [
                    SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                    SimpleField(name="document_id", type=SearchFieldDataType.String,
                                filterable=True),
                    SimpleField(name="client_id", type=SearchFieldDataType.String,
                                filterable=True),
                    SearchableField(name="text_content",
                                    type=SearchFieldDataType.String),
                    SimpleField(name="filename",    type=SearchFieldDataType.String),
                    SimpleField(name="source_name", type=SearchFieldDataType.String),
                    SimpleField(name="file_type",   type=SearchFieldDataType.String),
                    SimpleField(name="key_name",    type=SearchFieldDataType.String),
                    SearchField(
                        name="embedding",
                        type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                        searchable=True,
                        vector_search_dimensions=self._VECTOR_DIM,
                        vector_search_profile_name="vp",
                    ),
                ]
                try:
                    idx_client.get_index(index_name)
                except Exception:
                    idx_client.create_index(SearchIndex(
                        name=index_name, fields=fields,
                        vector_search=vector_search))

            self._docs_client = SearchClient(self._endpoint, "documents", cred)
            self._know_client = SearchClient(self._endpoint, "knowledge",  cred)
            self._available   = True
            logger.info("VectorStore: Azure AI Search backend ready at %s", self._endpoint)
        except ImportError:
            logger.warning(
                "VectorStore: azure-search-documents not installed. "
                "Run: pip install azure-search-documents")
        except Exception as exc:
            logger.warning("VectorStore: Azure AI Search init failed: %s", exc)

    @property
    def available(self) -> bool:
        """Whether the Azure AI Search clients initialized successfully."""
        return self._available

    def add_chunks(self, document_id: str, chunks: List[Dict]) -> int:
        """
        Embed and upload chunks for a document to the 'documents' index.

        Args:
            document_id: Owning document's ID (used to build chunk IDs and
                as the filterable `document_id` field).
            chunks: List of {"text": str, "metadata": dict}.

        Returns:
            Number of chunks uploaded, or 0 if unavailable/empty/on error.
        """
        if not self._available or not chunks:
            return 0
        texts = [c["text"] for c in chunks]
        embs  = _embed(texts)
        docs  = []
        for i, (chunk, emb) in enumerate(zip(chunks, embs)):
            meta = chunk.get("metadata", {})
            docs.append({
                "id":          f"{document_id}_{i}",
                "document_id": document_id,
                "text_content": chunk["text"],
                "embedding":   emb,
                "filename":    meta.get("filename", ""),
                "file_type":   meta.get("file_type", ""),
                "client_id":   meta.get("client_id", ""),
                "source_name": meta.get("source_name", ""),
            })
        try:
            self._docs_client.upload_documents(documents=docs)
            return len(docs)
        except Exception as exc:
            logger.warning("AzureSearchVectorStore.add_chunks: %s", exc)
            return 0

    def search_documents(self, query: str, n_results: int = 5,
                         where: Optional[Dict] = None,
                         doc_ids: Optional[set] = None) -> List[Dict]:
        """
        Vector search the 'documents' index for the most similar chunks.

        Args:
            query: Natural-language query, embedded before searching.
            n_results: Number of nearest neighbors to return.
            where: Optional {"client_id": str} filter (ignored when doc_ids set).
            doc_ids: When provided, restricts search to only these document IDs
                using an OData search.in filter — same semantics as the LanceDB
                and ChromaDB backends so Stage 2 scoped searches work on all backends.

        Returns:
            List of result dicts (text, document_id, filename, source_name,
            score, metadata), empty on error or if unavailable.
        """
        if not self._available:
            return []
        from azure.search.documents.models import VectorizedQuery
        try:
            q_emb = _embed_one(query)
            vq    = VectorizedQuery(vector=q_emb, k_nearest_neighbors=n_results,
                                    fields="embedding")
            if doc_ids:
                escaped = [d.replace("'", "''") for d in doc_ids]
                flt = f"search.in(document_id, '{','.join(escaped)}', ',')"
            elif where and where.get("client_id"):
                flt = f"client_id eq '{where['client_id']}'"
            else:
                flt = None
            results = self._docs_client.search(
                search_text=None, vector_queries=[vq],
                filter=flt, top=n_results)
            return [
                {
                    "text":        r["text_content"],
                    "document_id": r["document_id"],
                    "filename":    r.get("filename", ""),
                    "source_name": r.get("source_name", ""),
                    "score":       round(r["@search.score"], 4),
                    "metadata":    {"file_type": r.get("file_type", ""),
                                    "client_id": r.get("client_id", "")},
                }
                for r in results
            ]
        except Exception as exc:
            logger.warning("AzureSearchVectorStore.search_documents: %s", exc)
            return []

    def delete_document(self, document_id: str) -> bool:
        """Find and delete all index entries for a document_id. Returns True on success."""
        if not self._available:
            return False
        try:
            results = self._docs_client.search(
                search_text="*",
                filter=f"document_id eq '{document_id}'",
                select=["id"])
            ids = [{"id": r["id"]} for r in results]
            if ids:
                self._docs_client.delete_documents(documents=ids)
            return True
        except Exception as exc:
            logger.warning("AzureSearchVectorStore.delete_document: %s", exc)
            return False

    def document_chunk_count(self) -> int:
        """Return the 'documents' index's total document count (0 on error)."""
        if not self._available:
            return 0
        try:
            return self._docs_client.get_document_count()
        except Exception:
            return 0

    def set_knowledge(self, key: str, value: str,
                      metadata: Optional[Dict] = None) -> bool:
        """Embed and upsert a key/value entry into the 'knowledge' index."""
        if not self._available:
            return False
        try:
            emb = _embed_one(value)
            self._know_client.upload_documents(documents=[{
                "id": key, "key_name": key,
                "text_content": value, "embedding": emb,
            }])
            return True
        except Exception as exc:
            logger.warning("AzureSearchVectorStore.set_knowledge: %s", exc)
            return False

    def get_knowledge(self, key: str) -> Optional[str]:
        """Return the stored value for `key` from the 'knowledge' index, or None."""
        if not self._available:
            return None
        try:
            doc = self._know_client.get_document(key=key)
            return doc["text_content"] if doc else None
        except Exception:
            return None

    def search_knowledge(self, query: str, n_results: int = 5) -> List[Dict]:
        """Vector search the 'knowledge' index. Returns [{key, value, score, metadata}, ...]."""
        if not self._available:
            return []
        from azure.search.documents.models import VectorizedQuery
        try:
            q_emb = _embed_one(query)
            vq    = VectorizedQuery(vector=q_emb, k_nearest_neighbors=n_results,
                                    fields="embedding")
            results = self._know_client.search(
                search_text=None, vector_queries=[vq], top=n_results)
            return [
                {
                    "key":      r.get("key_name", r["id"]),
                    "value":    r["text_content"],
                    "score":    round(r["@search.score"], 4),
                    "metadata": {},
                }
                for r in results
            ]
        except Exception as exc:
            logger.warning("AzureSearchVectorStore.search_knowledge: %s", exc)
            return []

    def delete_knowledge(self, key: str) -> bool:
        """Delete a knowledge-store entry by key from the 'knowledge' index."""
        if not self._available:
            return False
        try:
            self._know_client.delete_documents(documents=[{"id": key}])
            return True
        except Exception:
            return False

    def list_knowledge_keys(self) -> List[str]:
        """Return all keys currently in the 'knowledge' index (empty list on error)."""
        if not self._available:
            return []
        try:
            res = self._know_client.search("*", select=["key_name"])
            return [r.get("key_name", "") for r in res if r.get("key_name")]
        except Exception:
            return []

    def stats(self) -> Dict:
        """Return backend identity + endpoint + availability + chunk/knowledge counts."""
        return {
            "backend":           "azure_ai_search",
            "endpoint":          self._endpoint,
            "available":         self._available,
            "document_chunks":   self.document_chunk_count(),
            "knowledge_entries": len(self.list_knowledge_keys()),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# LANCEDB BACKEND  (default local — real HNSW ANN index, no full table scan)
# ═══════════════════════════════════════════════════════════════════════════════

class _WriteJob:
    """Carries one write operation to the single LanceDB writer thread."""
    __slots__ = ("fn", "args", "kwargs", "result", "error", "done")

    def __init__(self, fn, args=(), kwargs=None):
        """
        Args:
            fn: Callable to run on the writer thread.
            args: Positional args for `fn`.
            kwargs: Keyword args for `fn` (defaults to {}).
        """
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs or {}
        self.result = None
        self.error  = None
        self.done   = threading.Event()

class _LanceDBVectorStore:
    """
    LanceDB-backed vector store.

    - Document chunks stored in a LanceDB table with HNSW cosine index.
    - Knowledge store delegated to _SQLVectorStore (small data, not worth moving).
    - Auto-migrates existing SQL embeddings on first use.
    - Falls back gracefully if lancedb is not installed (get_vector_store()
      then tries SQL Server instead).

    Con mitigations
    ---------------
    Two-store sync   : migrate_from_sql() called automatically when LanceDB
                       is empty but SQL has data; safe to call multiple times.
    Index threshold  : _maybe_index() only creates HNSW when >= 256 rows;
                       below that LanceDB uses brute-force (same accuracy,
                       index creation would raise an error anyway).
    HA / multi-node  : not solved for local deployment — set
                       AZURE_SEARCH_ENDPOINT to switch to Azure AI Search
                       when you need horizontal scale.
    """

    _MIN_INDEX_ROWS    = 5000  # only build index above this — below it brute-force is faster
    _INDEX_BATCH_ROWS  = 1000  # rebuild after this many new rows since last build (~60+ docs)

    def __init__(self):
        """Resolve the storage path, connect to LanceDB, and start the writer thread."""
        self._db                = None
        self._tbl               = None
        self._available         = False
        self._sql               = _SQLVectorStore()   # knowledge store + fallback
        self._path              = self._resolve_path()
        self._write_queue       = queue.Queue()
        self._rows_since_index  = 0   # rows added since last successful index build
        self._init()

    # ── Setup ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_path() -> str:
        """Return the LanceDB storage directory (LANCEDB_PATH env var, or Data/lancedb)."""
        env = os.environ.get("LANCEDB_PATH", "").strip()
        return env if env else os.path.join(_PROJECT_ROOT, "Data", "lancedb")

    def _init(self):
        """
        Connect to LanceDB, open/create the document_chunks table, and start
        the single background writer thread. Sets `self._available` True
        only once the writer thread is confirmed running; leaves it False
        (logging a warning) if lancedb isn't installed or init fails.
        """
        try:
            import lancedb  # noqa: F401
            os.makedirs(self._path, exist_ok=True)
            self._db    = lancedb.connect(self._path)
            self._tbl   = self._ensure_table()
            t = threading.Thread(
                target=self._writer_loop, daemon=True, name="lancedb-writer"
            )
            t.start()
            self._available = True   # only True once writer thread is confirmed running
            logger.info("VectorStore: LanceDB backend ready at %s", self._path)
        except ImportError:
            logger.warning(
                "VectorStore: lancedb not installed — run: pip install lancedb  "
                "(falling back to SQL Server)")
        except Exception as exc:
            logger.warning("VectorStore: LanceDB init failed: %s", exc)

    def _ensure_table(self):
        """
        Open the existing 'document_chunks' LanceDB table, or create it with
        the PyArrow schema (id, document_id, chunk_index, text_content,
        vector(384), filename, file_type, client_id, source_name) if absent.

        Returns:
            The LanceDB table handle.
        """
        import pyarrow as pa
        schema = pa.schema([
            pa.field("id",           pa.string()),
            pa.field("document_id",  pa.string()),
            pa.field("chunk_index",  pa.int32()),
            pa.field("text_content", pa.string()),
            pa.field("vector",       pa.list_(pa.float32(), 384)),
            pa.field("filename",     pa.string()),
            pa.field("file_type",    pa.string()),
            pa.field("client_id",    pa.string()),
            pa.field("source_name",  pa.string()),
        ])
        if "document_chunks" in self._db.table_names():
            return self._db.open_table("document_chunks")
        try:
            return self._db.create_table("document_chunks", schema=schema)
        except TypeError:
            # Older lancedb (<= 0.3): create_table requires data, not just schema
            import pandas as pd
            dummy = pd.DataFrame([{
                "id": "__init__", "document_id": "__init__", "chunk_index": 0,
                "text_content": "", "vector": [0.0] * 384,
                "filename": "", "file_type": "", "client_id": "", "source_name": "",
            }])
            tbl = self._db.create_table("document_chunks", data=dummy)
            tbl.delete("id = '__init__'")
            return tbl

    def _maybe_index(self):
        """Create or refresh IVF_PQ cosine index when we have enough rows."""
        try:
            n = self._tbl.count_rows()
            if n >= self._MIN_INDEX_ROWS:
                self._tbl.create_index(
                    metric="cosine",
                    vector_column_name="vector",
                    replace=True,
                )
                logger.info("LanceDB: index built/refreshed (%d rows)", n)
            # if below threshold, no index needed — brute-force is fast enough
        except Exception as exc:
            # Build failed — leave _rows_since_index as-is so next batch retries
            logger.warning("LanceDB: index build failed (fallback will handle searches): %s", exc)

    # ── Writer thread ─────────────────────────────────────────────────────────

    def _writer_loop(self):
        """Single background thread — every LanceDB write goes through here."""
        while True:
            job = self._write_queue.get()
            if job is None:          # shutdown signal
                self._write_queue.task_done()
                break
            try:
                job.result = job.fn(*job.args, **job.kwargs)
            except Exception as exc:
                job.error = exc
                logger.warning("LanceDB writer error: %s", exc)
            finally:
                job.done.set()
                self._write_queue.task_done()

    def _submit_write(self, fn, *args, **kwargs):
        """Queue a write job and block the caller until it completes."""
        job = _WriteJob(fn, args, kwargs)
        self._write_queue.put(job)
        job.done.wait()
        if job.error:
            raise job.error
        return job.result

    # ── Interface ─────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """Whether the LanceDB connection and writer thread initialized successfully."""
        return self._available

    def add_chunks(self, document_id: str, chunks: List[Dict]) -> int:
        """
        Embed and persist chunks for a document (replacing any prior chunks
        for the same document_id). Queued onto the single writer thread.

        Args:
            document_id: Owning document's ID.
            chunks: List of {"text": str, "metadata": dict}.

        Returns:
            Number of chunks stored, or 0 if unavailable/empty/on error.
        """
        if not self._available or not chunks:
            return 0
        return self._submit_write(self._add_chunks_impl, document_id, chunks)

    def _add_chunks_impl(self, document_id: str, chunks: List[Dict]) -> int:
        """Runs inside the writer thread — never call directly from outside."""
        texts = [c["text"] for c in chunks]
        try:
            embeddings = _embed(texts)
        except Exception as exc:
            logger.warning("LanceDB.add_chunks: embed failed: %s", exc)
            return 0

        rows = []
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            meta = chunk.get("metadata", {})
            rows.append({
                "id":           f"{document_id}_{i}",
                "document_id":  document_id,
                "chunk_index":  i,
                "text_content": chunk["text"],
                "vector":       [float(x) for x in emb],
                "filename":     meta.get("filename", ""),
                "file_type":    meta.get("file_type", ""),
                "client_id":    meta.get("client_id", ""),
                "source_name":  meta.get("source_name", ""),
            })
        try:
            # Delete old chunks then add — safe here because we're the only writer
            try:
                self._tbl.delete(f"document_id = '{document_id}'")
            except Exception:
                pass
            self._tbl.add(rows)
            self._rows_since_index += len(rows)
            if self._rows_since_index >= self._INDEX_BATCH_ROWS:
                self._maybe_index()
                self._rows_since_index = 0
            return len(rows)
        except Exception as exc:
            logger.warning("LanceDB.add_chunks: insert failed: %s", exc)
            return 0

    def search_documents(self, query: str, n_results: int = 5,
                         where: Optional[Dict] = None,
                         doc_ids: Optional[set] = None) -> List[Dict]:
        """
        Cosine ANN search over the document_chunks table (HNSW once indexed,
        brute-force below the row threshold).

        Args:
            query: Natural-language query, embedded before searching.
            n_results: Number of nearest neighbors to return.
            where: Optional {"client_id": str} filter (used only when
                `doc_ids` is not supplied).
            doc_ids: When provided, prefilters to only these document IDs
                (SQL-style WHERE) so target documents are always reachable
                regardless of corpus size.

        Returns:
            List of result dicts (text, document_id, filename, source_name,
            score [1 - cosine distance], metadata), empty on error.
        """
        if not self._available:
            return []
        try:
            q_emb = _embed_one(query)
        except Exception as exc:
            logger.warning("LanceDB.search_documents: embed failed: %s", exc)
            return []
        try:
            search = (self._tbl
                      .search(q_emb, vector_column_name="vector")
                      .metric("cosine")
                      .limit(n_results))

            if doc_ids:
                ids_lit = ", ".join(f"'{d.replace(chr(39), chr(39)*2)}'" for d in doc_ids)
                search  = search.where(f"document_id IN ({ids_lit})", prefilter=True)
            elif where and where.get("client_id"):
                cid    = where["client_id"].replace("'", "''")
                search = search.where(f"client_id = '{cid}'", prefilter=True)

            rows = search.to_list()
        except Exception as exc:
            # ANN search failed (e.g. broken/missing index file) — fall back to
            # brute-force pandas scan so the search still returns results.
            logger.warning("LanceDB.search_documents: ANN search failed (%s), falling back to brute-force", exc)
            return self._brute_force_search(q_emb, n_results, doc_ids, where)

        return self._format_rows(rows)

    def _format_rows(self, rows: list) -> List[Dict]:
        """Convert raw LanceDB ANN rows to the standard search_documents output format."""
        return [
            {
                "text":        r["text_content"],
                "document_id": r["document_id"],
                "filename":    r.get("filename", ""),
                "source_name": r.get("source_name", ""),
                "score":       round(max(0.0, 1.0 - float(r.get("_distance", 1.0))), 4),
                "metadata": {
                    "file_type":   r.get("file_type", ""),
                    "client_id":   r.get("client_id", ""),
                    "chunk_index": r.get("chunk_index"),
                },
            }
            for r in rows
        ]

    def _brute_force_search(self, q_emb: list, n_results: int,
                            doc_ids: Optional[set] = None,
                            where: Optional[Dict] = None) -> List[Dict]:
        """
        Full pandas table scan + Python cosine ranking.
        Used as fallback when the ANN index is broken or missing.
        Works regardless of index state because it reads data files directly.
        """
        try:
            import numpy as np
            df = self._tbl.to_pandas()

            if doc_ids:
                df = df[df["document_id"].isin(doc_ids)]
            elif where and where.get("client_id"):
                df = df[df["client_id"] == where["client_id"]]

            if df.empty:
                return []

            q = np.array(q_emb, dtype="float32")
            q_norm = q / (np.linalg.norm(q) + 1e-10)

            scored = []
            for _, row in df.iterrows():
                v = np.array(row["vector"], dtype="float32")
                norm = float(np.linalg.norm(v))
                score = max(0.0, float(q_norm @ (v / norm))) if norm > 1e-10 else 0.0
                scored.append((score, row))

            scored.sort(key=lambda x: x[0], reverse=True)

            return [
                {
                    "text":        row["text_content"],
                    "document_id": row["document_id"],
                    "filename":    row.get("filename", ""),
                    "source_name": row.get("source_name", ""),
                    "score":       round(s, 4),
                    "metadata": {
                        "file_type":   row.get("file_type", ""),
                        "client_id":   row.get("client_id", ""),
                        "chunk_index": row.get("chunk_index"),
                    },
                }
                for s, row in scored[:n_results]
            ]
        except Exception as exc:
            logger.warning("LanceDB._brute_force_search: fallback also failed: %s", exc)
            return []

    def delete_document(self, document_id: str) -> bool:
        """Delete all chunks for a document_id (queued onto the writer thread)."""
        if not self._available:
            return False
        return self._submit_write(self._delete_document_impl, document_id)

    def _delete_document_impl(self, document_id: str) -> bool:
        """Runs inside the writer thread — never call directly from outside."""
        try:
            self._tbl.delete(f"document_id = '{document_id}'")
            return True
        except Exception as exc:
            logger.warning("LanceDB.delete_document: %s", exc)
            return False

    def document_chunk_count(self) -> int:
        """Return the LanceDB table's total row count (0 on error)."""
        if not self._available:
            return 0
        try:
            return self._tbl.count_rows()
        except Exception:
            return 0

    # Knowledge store — delegate to SQL (tiny data, not worth a second table)
    def set_knowledge(self, key: str, value: str,
                      metadata: Optional[Dict] = None) -> bool:
        """Delegate to the internal `_SQLVectorStore` — KV data is too small to justify a LanceDB table."""
        return self._sql.set_knowledge(key, value, metadata)

    def get_knowledge(self, key: str) -> Optional[str]:
        """Delegate to the internal `_SQLVectorStore`."""
        return self._sql.get_knowledge(key)

    def search_knowledge(self, query: str, n_results: int = 5) -> List[Dict]:
        """Delegate to the internal `_SQLVectorStore`."""
        return self._sql.search_knowledge(query, n_results)

    def delete_knowledge(self, key: str) -> bool:
        """Delegate to the internal `_SQLVectorStore`."""
        return self._sql.delete_knowledge(key)

    def list_knowledge_keys(self) -> List[str]:
        """Delegate to the internal `_SQLVectorStore`."""
        return self._sql.list_knowledge_keys()

    # ── Migration ─────────────────────────────────────────────────────────────

    def migrate_from_sql(self) -> int:
        """
        Copy existing embeddings from app_document_chunks → LanceDB.
        Safe to call multiple times — skips document IDs already present.
        Returns number of chunks migrated.
        """
        if not self._available:
            return 0
        return self._submit_write(self._migrate_from_sql_impl)

    def _migrate_from_sql_impl(self) -> int:
        """Runs inside the writer thread — never call directly from outside."""
        try:
            # Find doc IDs already in LanceDB
            existing: set = set()
            try:
                arrow = self._tbl.to_arrow(columns=["document_id"])
                existing = set(arrow.column("document_id").to_pylist())
            except Exception:
                pass

            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, document_id, chunk_index, text_content, embedding,
                           filename, file_type, client_id, source_name
                    FROM app_document_chunks
                """)
                cols     = [d[0] for d in cur.description]
                sql_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            to_migrate = [r for r in sql_rows if r["document_id"] not in existing]
            if not to_migrate:
                logger.info("LanceDB.migrate_from_sql: nothing to migrate")
                return 0

            lance_rows = []
            for r in to_migrate:
                raw = r.get("embedding")
                if not raw:
                    continue
                try:
                    emb = json.loads(raw) if isinstance(raw, str) else raw
                    if not emb or len(emb) != 384:
                        continue
                except Exception:
                    continue
                lance_rows.append({
                    "id":           r["id"],
                    "document_id":  r["document_id"],
                    "chunk_index":  int(r.get("chunk_index") or 0),
                    "text_content": r.get("text_content") or "",
                    "vector":       [float(x) for x in emb],
                    "filename":     r.get("filename") or "",
                    "file_type":    r.get("file_type") or "",
                    "client_id":    r.get("client_id") or "",
                    "source_name":  r.get("source_name") or "",
                })

            if lance_rows:
                # Insert in batches to avoid memory spikes
                batch_size = 500
                for i in range(0, len(lance_rows), batch_size):
                    self._tbl.add(lance_rows[i:i + batch_size])
                self._maybe_index()
                logger.info("LanceDB.migrate_from_sql: migrated %d chunks", len(lance_rows))

            return len(lance_rows)
        except Exception as exc:
            logger.warning("LanceDB.migrate_from_sql: %s", exc)
            return 0

    def stats(self) -> Dict:
        """Return backend identity + storage path + availability + chunk/knowledge counts."""
        return {
            "backend":           "lancedb",
            "path":              self._path,
            "available":         self._available,
            "document_chunks":   self.document_chunk_count(),
            "knowledge_entries": len(self.list_knowledge_keys()),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# VectorStore  — public type alias + singleton factory
# ═══════════════════════════════════════════════════════════════════════════════

# Type alias — LanceDB is the canonical local backend; Azure AI Search for cloud
VectorStore = _LanceDBVectorStore

_vs      = None
_vs_lock = threading.Lock()


def get_vector_store():
    """
    Returns the active VectorStore instance.

    Selection order:
      1. Azure AI Search — when AZURE_SEARCH_ENDPOINT + AZURE_SEARCH_KEY are set
      2. LanceDB         — default local (real ANN index, required dependency)

    Note: SQL Server is NOT used as a vector backend fallback. lancedb is a
    required dependency (pip install lancedb). SQL Server is still used for
    document metadata, access control, knowledge store, cache, and traces —
    none of that goes through this function.
    """
    global _vs
    if _vs is not None:
        return _vs

    with _vs_lock:
        if _vs is not None:   # double-checked — another thread may have initialized while we waited
            return _vs

        # Priority 1: Azure AI Search
        if os.environ.get("AZURE_SEARCH_ENDPOINT") and os.environ.get("AZURE_SEARCH_KEY"):
            candidate = _AzureSearchVectorStore()
            if candidate.available:
                _vs = candidate
                return _vs
            logger.warning(
                "VectorStore: Azure AI Search configured but unavailable — falling back to LanceDB")

        # Priority 2: LanceDB
        candidate = _LanceDBVectorStore()
        if not candidate.available:
            raise RuntimeError(
                "VectorStore: LanceDB is unavailable. "
                "Run: pip install lancedb  (it is a required dependency)")

        # Auto-migrate existing SQL embeddings on first startup (serialized via writer thread)
        if candidate.document_chunk_count() == 0:
            migrated = candidate.migrate_from_sql()
            if migrated:
                logger.info(
                    "VectorStore: auto-migrated %d chunks from SQL Server -> LanceDB", migrated)

        _vs = candidate
        return _vs
