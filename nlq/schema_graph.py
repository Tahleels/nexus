"""
schema_graph.py — Schema graph layer for join-path discovery and edge-weight learning.

Builds a graph of Table/Column nodes connected by inferred foreign-key
("JOINS_TO") edges, used by nlq_engine.hybrid_schema_pruning() to:
  - discover bridge tables that connect two otherwise-unrelated tables
    selected by semantic search (find_join_path / get_join_hints)
  - persist table-level embeddings so semantic_table_selection() can skip
    re-encoding on restart (get_table_embeddings)
  - reinforce join paths that produced non-empty result sets over time
    (update_edge_weights)

Two backend implementations are provided behind the same public API
(enabled, build_graph, delete_agent_graph, find_join_path, get_join_hints,
get_table_embeddings, update_edge_weights, needs_rebuild):

  1. SchemaGraph — Neo4j-backed. Enabled only when NEO4J_URI, NEO4J_USER,
     and NEO4J_PASSWORD env vars are set and the server is reachable.
       NEO4J_URI      e.g. bolt://localhost:7687
       NEO4J_USER     e.g. neo4j
       NEO4J_PASSWORD e.g. password
  2. NetworkXSchemaGraph — SQL Server (app_schema_graph table) + in-memory
     networkx.DiGraph. Used automatically when Neo4j is not configured;
     always available locally since it only needs SQL Server + networkx.

get_schema_graph() returns a module-level singleton, picking whichever
backend is available (Neo4j > NetworkX > disabled no-op).

All public methods on both classes are safe no-ops when their backend is
unavailable, so callers (nlq_engine.py) never need to guard against
ImportError or a missing connection.
"""

import hashlib
import json
from logging_config import get_logger
import os
import re
from typing import Dict, List, Optional

logger = get_logger(__name__)

try:
    from neo4j import GraphDatabase
    HAS_NEO4J = True
except ImportError:
    HAS_NEO4J = False
    logger.warning("SchemaGraph: neo4j package not installed — graph layer disabled")


class SchemaGraph:
    """
    Neo4j-backed schema graph.  All public methods are no-ops when Neo4j
    is unavailable so callers never need to guard against ImportError.
    """

    # Regex: JOIN table [alias] ON lhs = rhs
    _JOIN_ON_RE = re.compile(
        r'(?:INNER\s+|LEFT\s+(?:OUTER\s+)?|RIGHT\s+(?:OUTER\s+)?|FULL\s+(?:OUTER\s+)?|CROSS\s+)?'
        r'JOIN\s+([\w.\[\]"]+)(?:\s+(?:AS\s+)?[\w]+)?\s+ON\s+'
        r'([\w.\[\]"]+)\s*=\s*([\w.\[\]"]+)',
        re.IGNORECASE,
    )

    def __init__(self):
        self._driver = None
        self._enabled = False

        if not HAS_NEO4J:
            return

        uri = os.getenv("NEO4J_URI")
        user = os.getenv("NEO4J_USER")
        password = os.getenv("NEO4J_PASSWORD")

        if not (uri and user and password):
            logger.warning(
                "SchemaGraph disabled: set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD to enable"
            )
            return

        try:
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            self._driver.verify_connectivity()
            self._enabled = True
            logger.info("SchemaGraph: connected to Neo4j at %s", uri)
        except Exception as exc:
            logger.warning("SchemaGraph disabled: Neo4j unreachable — %s", exc)
            self._driver = None

    # ------------------------------------------------------------------
    # Public properties / lifecycle
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when a live Neo4j connection was established at construction time."""
        return self._enabled

    def close(self) -> None:
        """Close the underlying Neo4j driver, if one was opened."""
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Hash / staleness
    # ------------------------------------------------------------------

    @staticmethod
    def _schema_hash(schema_context: Dict) -> str:
        """Return a stable SHA-256 hex digest of schema_context (key order independent)."""
        raw = json.dumps(schema_context, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()

    def needs_rebuild(self, agent_name: str, schema_context: Dict) -> bool:
        """True when the graph doesn't exist yet or the schema hash has changed."""
        if not self._enabled:
            return False
        try:
            current_hash = self._schema_hash(schema_context)
            with self._driver.session() as sess:
                count = sess.run(
                    "MATCH (t:Table {agent_name:$a}) RETURN count(t) AS n",
                    a=agent_name,
                ).single()["n"]
                if count == 0:
                    return True
                meta = sess.run(
                    "MATCH (m:AgentMeta {agent_name:$a}) RETURN m.schema_hash AS h",
                    a=agent_name,
                ).single()
                return meta is None or meta["h"] != current_hash
        except Exception as exc:
            logger.warning("SchemaGraph.needs_rebuild error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def build_graph(
        self,
        agent_name: str,
        schema_context: Dict,
        embedding_model=None,
    ) -> None:
        """Build graph for agent — skips silently when already up-to-date."""
        if not self._enabled:
            return
        try:
            if not self.needs_rebuild(agent_name, schema_context):
                logger.debug("SchemaGraph: '%s' up-to-date, skipping rebuild", agent_name)
                return
            logger.info("SchemaGraph: building graph for agent '%s'", agent_name)
            self._delete_nodes(agent_name)
            self._build_nodes_and_edges(agent_name, schema_context, embedding_model)
            with self._driver.session() as sess:
                sess.run(
                    "MERGE (m:AgentMeta {agent_name:$a}) SET m.schema_hash = $h",
                    a=agent_name,
                    h=self._schema_hash(schema_context),
                )
            logger.info("SchemaGraph: graph built for '%s'", agent_name)
        except Exception as exc:
            logger.warning("SchemaGraph.build_graph failed for '%s': %s", agent_name, exc)

    def delete_agent_graph(self, agent_name: str) -> None:
        """Remove all graph nodes for an agent (call on PUT/DELETE agent)."""
        if not self._enabled:
            return
        try:
            self._delete_nodes(agent_name)
        except Exception as exc:
            logger.warning("SchemaGraph.delete_agent_graph failed: %s", exc)

    def _delete_nodes(self, agent_name: str) -> None:
        """Detach-delete every node tagged with this agent_name (Table, Column, AgentMeta)."""
        with self._driver.session() as sess:
            sess.run("MATCH (n {agent_name:$a}) DETACH DELETE n", a=agent_name)

    def _build_nodes_and_edges(
        self,
        agent_name: str,
        schema_context: Dict,
        embedding_model=None,
    ) -> None:
        """Create Table/Column nodes and HAS_COLUMN/JOINS_TO edges for one agent's schema.

        Foreign keys are inferred heuristically: a column ending in ``_id``/``Id``
        (other than the literal ``id`` PK) is matched against a fuzzy, pluralization-
        stripped map of table names (e.g. ``customer_id`` → ``Customers``) to create
        a ``JOINS_TO`` edge with an initial weight of 1.0.
        """
        tables = schema_context.get("tables", [])

        # Build a fuzzy lookup: stripped-lowercase table name → original name
        table_names = [t["table_name"] for t in tables]
        fuzzy_map: Dict[str, str] = {}
        for tn in table_names:
            # strip schema prefix (dbo.Orders → orders), lowercase, strip trailing 's'
            bare = tn.split(".")[-1].lower().rstrip("s")
            fuzzy_map[bare] = tn

        with self._driver.session() as sess:
            for table in tables:
                tname = table["table_name"]
                desc = table.get("description", "")
                purpose = table.get("estimated_purpose", "")
                cols = [c.get("name", "") for c in table.get("columns", [])]
                table_text = f"{tname} {desc} {purpose} {' '.join(cols)}".lower()
                emb = self._compute_embedding(table_text, embedding_model)

                sess.run(
                    """
                    MERGE (t:Table {agent_name:$a, table_name:$tn})
                    SET t.description=$desc, t.estimated_purpose=$purpose, t.emb=$emb
                    """,
                    a=agent_name, tn=tname, desc=desc, purpose=purpose, emb=emb,
                )

                for col in table.get("columns", []):
                    cname = col.get("name", "")
                    if not cname:
                        continue
                    ctype = col.get("type", "")
                    inferred = col.get("inferred_purpose", "")
                    col_text = f"{tname} {cname} {ctype} {inferred}".lower()
                    col_emb = self._compute_embedding(col_text, embedding_model)

                    sess.run(
                        """
                        MERGE (c:Column {agent_name:$a, table_name:$tn, column_name:$cn})
                        SET c.type=$ct, c.inferred_purpose=$ip, c.emb=$emb
                        """,
                        a=agent_name, tn=tname, cn=cname, ct=ctype, ip=inferred, emb=col_emb,
                    )
                    sess.run(
                        """
                        MATCH (t:Table  {agent_name:$a, table_name:$tn})
                        MATCH (c:Column {agent_name:$a, table_name:$tn, column_name:$cn})
                        MERGE (t)-[:HAS_COLUMN]->(c)
                        """,
                        a=agent_name, tn=tname, cn=cname,
                    )

                    # Detect FK: column ends in _id / Id (but is not the PK itself)
                    is_fk = (
                        re.search(r'_[Ii]d$|Id$', cname)
                        or "foreign key" in inferred.lower()
                        or "reference" in inferred.lower()
                    )
                    if is_fk and cname.lower() not in ("id",):
                        stripped = re.sub(r"_?[Ii]d$", "", cname).lower().rstrip("s")
                        ref_table = fuzzy_map.get(stripped)
                        if ref_table and ref_table != tname:
                            sess.run(
                                """
                                MATCH (src:Table {agent_name:$a, table_name:$src})
                                MATCH (dst:Table {agent_name:$a, table_name:$dst})
                                MERGE (src)-[r:JOINS_TO {key_col:$kc, ref_col:'id'}]->(dst)
                                ON CREATE SET r.weight = 1.0
                                """,
                                a=agent_name, src=tname, dst=ref_table, kc=cname,
                            )

    @staticmethod
    def _compute_embedding(text: str, embedding_model) -> Optional[List[float]]:
        """Encode text with embedding_model and return a plain list of floats, or None on failure/absence."""
        if embedding_model is None:
            return None
        try:
            return embedding_model.encode(text).tolist()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Query-time helpers
    # ------------------------------------------------------------------

    def find_join_path(self, agent_name: str, tables: List[str]) -> List[str]:
        """
        BFS up to 2 hops to discover bridge tables linking non-adjacent anchor tables.
        Returns additional table names to include (not already in `tables`).
        """
        if not self._enabled or len(tables) < 2:
            return []
        bridge: set = set()
        try:
            with self._driver.session() as sess:
                for i in range(len(tables)):
                    for j in range(i + 1, len(tables)):
                        t1, t2 = tables[i], tables[j]
                        rec = sess.run(
                            """
                            MATCH path=(a:Table {agent_name:$an,table_name:$t1})
                                       -[:JOINS_TO*1..2]-
                                      (b:Table {agent_name:$an,table_name:$t2})
                            RETURN [n IN nodes(path) | n.table_name] AS tables,
                                   [r IN relationships(path) | {key:r.key_col,ref:r.ref_col,w:r.weight}] AS joins
                            ORDER BY size(nodes(path)) ASC LIMIT 1
                            """,
                            an=agent_name, t1=t1, t2=t2,
                        ).single()
                        if rec:
                            for t in rec["tables"]:
                                if t not in tables:
                                    bridge.add(t)
        except Exception as exc:
            logger.warning("SchemaGraph.find_join_path failed: %s", exc)
        return list(bridge)

    def get_join_hints(self, agent_name: str, tables: List[str]) -> str:
        """
        Returns a formatted block of known join paths between the given tables,
        ready to append to the schema string.  Empty string on failure.
        """
        if not self._enabled or not tables:
            return ""
        try:
            with self._driver.session() as sess:
                recs = sess.run(
                    """
                    MATCH (a:Table {agent_name:$an})-[r:JOINS_TO]->(b:Table {agent_name:$an})
                    WHERE a.table_name IN $tables AND b.table_name IN $tables
                    RETURN a.table_name AS src, r.key_col AS key,
                           b.table_name AS dst, r.ref_col AS ref, r.weight AS w
                    ORDER BY r.weight DESC
                    """,
                    an=agent_name, tables=tables,
                ).data()
                if not recs:
                    return ""
                lines = [
                    f"  {r['src']}.{r['key']} → {r['dst']}.{r['ref']} [score: {r['w']:.1f}]"
                    for r in recs
                ]
                return "--- JOIN PATHS (use these exact keys) ---\n" + "\n".join(lines)
        except Exception as exc:
            logger.warning("SchemaGraph.get_join_hints failed: %s", exc)
        return ""

    def get_table_embeddings(self, agent_name: str) -> Dict[str, Optional[List[float]]]:
        """Fetch stored table-level embeddings for an agent (returns {} on failure)."""
        if not self._enabled:
            return {}
        try:
            with self._driver.session() as sess:
                return {
                    rec["tn"]: rec["emb"]
                    for rec in sess.run(
                        "MATCH (t:Table {agent_name:$a}) RETURN t.table_name AS tn, t.emb AS emb",
                        a=agent_name,
                    )
                }
        except Exception as exc:
            logger.warning("SchemaGraph.get_table_embeddings failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Edge-weight learning
    # ------------------------------------------------------------------

    def update_edge_weights(self, agent_name: str, sql: str, row_count: int) -> None:
        """
        Parse SQL for JOIN…ON patterns, then increment/decrement edge weights.
        delta = +0.1 if row_count > 0, else -0.05; clamped to [0.1, 3.0].
        """
        if not self._enabled or not sql:
            return
        try:
            delta = 0.1 if row_count > 0 else -0.05
            for m in self._JOIN_ON_RE.finditer(sql):
                lhs_parts = m.group(2).replace("[", "").replace("]", "").replace('"', "").split(".")
                rhs_parts = m.group(3).replace("[", "").replace("]", "").replace('"', "").split(".")
                t1 = lhs_parts[-2] if len(lhs_parts) >= 2 else None
                t2 = rhs_parts[-2] if len(rhs_parts) >= 2 else None
                if not t1 or not t2 or t1 == t2:
                    continue
                with self._driver.session() as sess:
                    for src, dst in ((t1, t2), (t2, t1)):
                        sess.run(
                            "MATCH (a:Table {agent_name:$an,table_name:$s})"
                            "-[r:JOINS_TO]->"
                            "(b:Table {agent_name:$an,table_name:$d}) "
                            "SET r.weight = min(3.0, max(0.1, r.weight + $delta))",
                            an=agent_name, s=src, d=dst, delta=delta,
                        )
        except Exception as exc:
            logger.warning("SchemaGraph.update_edge_weights failed: %s", exc)


# ---------------------------------------------------------------------------
# NetworkX + SQL Server fallback (no Neo4j required)
# ---------------------------------------------------------------------------

try:
    import networkx as nx
    HAS_NX = True
except ImportError:
    HAS_NX = False
    logger.warning("SchemaGraph: networkx not installed — SQL graph fallback disabled")


class NetworkXSchemaGraph:
    """
    Drop-in replacement for SchemaGraph that uses SQL Server + NetworkX.

    Storage  : app_schema_graph table (created by app_db.ensure_tables)
    In-memory: networkx.DiGraph rebuilt from SQL on each build_graph() call
    Azure    : table migrates to Azure SQL Database with zero code changes.
               For a proper graph DB later, swap to Azure Cosmos DB Gremlin
               (same public API, different _load_graph / _save_edge calls).

    Enabled  : always True when networkx is installed AND SQL Server is reachable.
    """

    def __init__(self):
        self._graphs: Dict[str, object] = {}   # agent_name → nx.DiGraph
        self._enabled = HAS_NX
        if not HAS_NX:
            logger.warning("NetworkXSchemaGraph: networkx not installed")
        # Pre-load all existing agents' graphs
        if self._enabled:
            self._load_all_graphs()

    @property
    def enabled(self) -> bool:
        """True when networkx is importable (this backend has no external server dependency)."""
        return self._enabled

    def close(self) -> None:
        """No-op — present only to mirror SchemaGraph's lifecycle API."""
        pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_db(self):
        """Return a SQL Server connection context manager via app_db.get_app_db()."""
        from app_db import get_app_db
        return get_app_db()

    def _load_graph(self, agent_name: str) -> "nx.DiGraph":
        """Rebuild one agent's nx.DiGraph from its rows in app_schema_graph."""
        G = nx.DiGraph()
        if not HAS_NX:
            return G
        try:
            with self._get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT table_a, table_b, join_key, ref_key, weight
                    FROM   app_schema_graph
                    WHERE  agent_name = ?
                """, agent_name)
                for row in cur.fetchall():
                    ta, tb, jk, rk, w = row
                    G.add_edge(ta, tb, join_key=jk, ref_key=rk, weight=float(w))
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph._load_graph failed for '%s': %s", agent_name, exc)
        return G

    def _load_all_graphs(self) -> None:
        """Pre-load every agent's graph from app_schema_graph into memory at startup."""
        if not HAS_NX:
            return
        try:
            with self._get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT agent_name FROM app_schema_graph")
                names = [r[0] for r in cur.fetchall()]
            for name in names:
                self._graphs[name] = self._load_graph(name)
            if names:
                logger.info("NetworkXSchemaGraph: loaded graphs for %d agents", len(names))
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph._load_all_graphs failed: %s", exc)

    def _save_edge(self, agent_name: str, table_a: str, table_b: str,
                   join_key: str, ref_key: str, weight: float,
                   schema_hash: str) -> None:
        """Upsert one JOINS_TO edge row into app_schema_graph (MERGE on agent/table_a/table_b/join_key)."""
        try:
            with self._get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    MERGE app_schema_graph AS t
                    USING (SELECT ? AS an, ? AS ta, ? AS tb, ? AS jk) AS s
                          ON t.agent_name = s.an AND t.table_a = s.ta
                         AND t.table_b = s.tb   AND t.join_key = s.jk
                    WHEN MATCHED THEN
                        UPDATE SET weight = ?, schema_hash = ?
                    WHEN NOT MATCHED THEN
                        INSERT (agent_name, table_a, table_b, join_key, ref_key, weight, schema_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                """, agent_name, table_a, table_b, join_key,
                    weight, schema_hash,
                    agent_name, table_a, table_b, join_key, ref_key, weight, schema_hash)
                conn.commit()
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph._save_edge failed: %s", exc)

    # ── Public API (mirrors SchemaGraph) ─────────────────────────────────────

    def needs_rebuild(self, agent_name: str, schema_context: Dict) -> bool:
        """True when no graph exists yet for agent_name, or its stored schema_hash is stale."""
        current_hash = self._schema_hash(schema_context)
        try:
            with self._get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT TOP 1 schema_hash FROM app_schema_graph
                    WHERE agent_name = ?
                """, agent_name)
                row = cur.fetchone()
            if row is None:
                return True
            return row[0] != current_hash
        except Exception:
            return True

    @staticmethod
    def _schema_hash(schema_context: Dict) -> str:
        """Return a 32-char SHA-256 prefix of schema_context (fits the schema_hash column width)."""
        import hashlib as _hl
        raw = json.dumps(schema_context, sort_keys=True).encode()
        return _hl.sha256(raw).hexdigest()[:32]

    def build_graph(self, agent_name: str, schema_context: Dict,
                    embedding_model=None) -> None:
        """Build or refresh graph for agent from schema_context."""
        if not self._enabled:
            return
        try:
            if not self.needs_rebuild(agent_name, schema_context):
                logger.debug("NetworkXSchemaGraph: '%s' up-to-date", agent_name)
                return
            logger.info("NetworkXSchemaGraph: building graph for '%s'", agent_name)
            self.delete_agent_graph(agent_name)
            schema_hash = self._schema_hash(schema_context)

            tables     = schema_context.get("tables", [])
            table_names = [t["table_name"] for t in tables]
            # Build fuzzy FK map: bare-name → full table name
            fuzzy: Dict[str, str] = {
                t.split(".")[-1].lower().rstrip("s"): t for t in table_names
            }

            G = nx.DiGraph()
            for table in tables:
                tname = table["table_name"]
                for col in table.get("columns", []):
                    cname = col.get("name", "")
                    if not cname:
                        continue
                    inferred = col.get("inferred_purpose", "").lower()
                    is_fk = (
                        re.search(r"_[Ii]d$|Id$", cname)
                        or "foreign key" in inferred
                        or "reference" in inferred
                    )
                    if is_fk and cname.lower() not in ("id",):
                        bare     = re.sub(r"_?[Ii]d$", "", cname).lower().rstrip("s")
                        ref_tbl  = fuzzy.get(bare)
                        if ref_tbl and ref_tbl != tname:
                            G.add_edge(tname, ref_tbl,
                                       join_key=cname, ref_key="id", weight=1.0)
                            self._save_edge(agent_name, tname, ref_tbl,
                                            cname, "id", 1.0, schema_hash)

            self._graphs[agent_name] = G
            logger.info("NetworkXSchemaGraph: '%s' built (%d edges)", agent_name, G.number_of_edges())
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph.build_graph failed: %s", exc)

    def delete_agent_graph(self, agent_name: str) -> None:
        """Remove agent_name's rows from app_schema_graph and drop its in-memory graph."""
        if not self._enabled:
            return
        try:
            with self._get_db() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM app_schema_graph WHERE agent_name = ?", agent_name)
                conn.commit()
            self._graphs.pop(agent_name, None)
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph.delete_agent_graph failed: %s", exc)

    def find_join_path(self, agent_name: str, tables: List[str]) -> List[str]:
        """BFS up to 2 hops to discover bridge tables between anchor tables."""
        if not self._enabled or len(tables) < 2:
            return []
        G = self._graphs.get(agent_name)
        if G is None or G.number_of_nodes() == 0:
            return []
        bridge: set = set()
        try:
            for i in range(len(tables)):
                for j in range(i + 1, len(tables)):
                    t1, t2 = tables[i], tables[j]
                    if not G.has_node(t1) or not G.has_node(t2):
                        continue
                    try:
                        path = nx.shortest_path(G.to_undirected(), t1, t2)
                        for t in path:
                            if t not in tables:
                                bridge.add(t)
                    except nx.NetworkXNoPath:
                        pass
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph.find_join_path failed: %s", exc)
        return list(bridge)

    def get_join_hints(self, agent_name: str, tables: List[str]) -> str:
        """Return formatted join-path hints for the schema prompt."""
        if not self._enabled or not tables:
            return ""
        G = self._graphs.get(agent_name)
        if G is None:
            return ""
        lines: List[str] = []
        try:
            for src, dst, data in G.edges(data=True):
                if src in tables and dst in tables:
                    jk = data.get("join_key", "?")
                    rk = data.get("ref_key",  "id")
                    w  = data.get("weight",   1.0)
                    lines.append(f"  {src}.{jk} → {dst}.{rk} [score: {w:.1f}]")
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph.get_join_hints failed: %s", exc)
        if not lines:
            return ""
        return "--- JOIN PATHS (use these exact keys) ---\n" + "\n".join(lines)

    def get_table_embeddings(self, agent_name: str) -> Dict[str, Optional[List[float]]]:
        """Always returns {} — this backend persists embeddings in app_schema_embedding_cache
        (SQL Server) instead, which nlq_engine loads directly without going through the graph."""
        return {}

    def update_edge_weights(self, agent_name: str, sql: str, row_count: int) -> None:
        """Parse SQL JOINs and nudge edge weights up/down based on query success."""
        if not self._enabled or not sql:
            return
        G = self._graphs.get(agent_name)
        if G is None:
            return
        _JOIN_RE = re.compile(
            r"(?:INNER\s+|LEFT\s+(?:OUTER\s+)?|RIGHT\s+(?:OUTER\s+)?)?"
            r"JOIN\s+([\w.\[\]\"]+)(?:\s+(?:AS\s+)?[\w]+)?\s+ON\s+"
            r"([\w.\[\]\"]+)\s*=\s*([\w.\[\]\"]+)",
            re.IGNORECASE,
        )
        delta = 0.1 if row_count > 0 else -0.05
        try:
            for m in _JOIN_RE.finditer(sql):
                lhs = m.group(2).replace("[","").replace("]","").replace('"',"").split(".")
                rhs = m.group(3).replace("[","").replace("]","").replace('"',"").split(".")
                t1  = lhs[-2] if len(lhs) >= 2 else None
                t2  = rhs[-2] if len(rhs) >= 2 else None
                if not t1 or not t2 or t1 == t2:
                    continue
                for src, dst in ((t1, t2), (t2, t1)):
                    if G.has_edge(src, dst):
                        old_w = G[src][dst].get("weight", 1.0)
                        new_w = max(0.1, min(3.0, old_w + delta))
                        G[src][dst]["weight"] = new_w
                        jk = G[src][dst].get("join_key", "")
                        rk = G[src][dst].get("ref_key",  "id")
                        schema_hash = ""
                        try:
                            with self._get_db() as conn:
                                cur = conn.cursor()
                                cur.execute("""
                                    UPDATE app_schema_graph
                                    SET weight = ?
                                    WHERE agent_name = ? AND table_a = ? AND table_b = ?
                                      AND join_key = ?
                                """, new_w, agent_name, src, dst, jk)
                                conn.commit()
                        except Exception:
                            pass
        except Exception as exc:
            logger.warning("NetworkXSchemaGraph.update_edge_weights failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_graph: Optional[object] = None


def get_schema_graph():
    """
    Returns the best available schema graph implementation:
      1. Neo4j SchemaGraph  — when NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD are set
      2. NetworkXSchemaGraph — SQL Server + NetworkX (always available locally)
      3. Disabled SchemaGraph — if networkx is also missing (all ops are no-ops)
    """
    global _graph
    if _graph is not None:
        return _graph

    # Try Neo4j first (existing class)
    neo4j_graph = SchemaGraph()
    if neo4j_graph.enabled:
        _graph = neo4j_graph
        return _graph

    # Fall back to NetworkX + SQL Server
    if HAS_NX:
        _graph = NetworkXSchemaGraph()
        logger.info("SchemaGraph: using NetworkX + SQL Server backend")
    else:
        _graph = neo4j_graph  # disabled no-op instance
        logger.warning("SchemaGraph: all backends disabled (no Neo4j creds, no networkx)")

    return _graph
