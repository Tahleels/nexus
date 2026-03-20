"""Base workflow execution engine for Hub Workflows.

Implements graph-based workflow execution: nodes (agent calls, SQL queries,
HTTP requests, conditions, transforms, variable assignment, delays, output)
connected by edges, run either sequentially (topologically sorted, with
conditional branch skipping) or in parallel (agent nodes only, fan-out/fan-in).

``blueprints/agents_hub_bp.py`` defines ``HubWorkflowEngine``, a parallel,
fully self-contained implementation that delegates the variable/condition/
topo-sort helpers here but adds many more node types (db_read/db_write,
file_read/file_write, email_read/email_send, loop_start/loop_end, approval)
plus SQL-Server-backed agent lookup and human-in-the-loop pause/resume. This
module's own ``_run_sequential``/``_run_parallel`` are a simpler standalone
path (used when HubWorkflowEngine is not the caller) that loads agents via
``app_db`` and only supports the smaller node-type set defined below.
"""
import json, time, re
from datetime import datetime


class WorkflowEngine:
    """Executes a workflow graph (nodes + edges) sequentially or in parallel.

    Supports node types: trigger, agent, query_db, condition, transform,
    http_request, set_variable, delay, output. Agent nodes look up their
    agent record from the local ``hub_agents`` table via ``app_db`` (SQLite),
    and run it through ``core.orchestrator.engine.Orchestrator``.
    """

    def __init__(self, api_key):
        """Args:
            api_key: OpenAI API key forwarded to any ``Orchestrator`` created
                for agent nodes.
        """
        self.api_key = api_key

    # ── Public entry point ────────────────────────────────────────────────────

    def execute(self, workflow, input_data):
        """Run a workflow to completion, streaming NDJSON progress events.

        Args:
            workflow: Workflow dict with ``nodes``, ``edges``, ``name``, and
                ``execution_mode`` ("sequential" or "parallel") keys.
            input_data: The trigger input value fed to the first node(s).

        Yields:
            JSON-encoded (NDJSON) strings for each lifecycle/node event,
            starting with a ``wf_start`` event.
        """
        nodes = workflow.get('nodes', [])
        edges = workflow.get('edges', [])
        mode  = workflow.get('execution_mode', 'sequential')
        yield json.dumps({"type": "wf_start", "workflow": workflow.get('name', ''),
                          "nodes": len(nodes)}) + '\n'
        if mode == 'parallel':
            yield from self._run_parallel(nodes, input_data)
        else:
            yield from self._run_sequential(nodes, edges, input_data)

    # ── Variable helpers ──────────────────────────────────────────────────────

    def _substitute_vars(self, template, context):
        """Replace {{var_name}} tokens with values from context['vars']."""
        if not template:
            return ''
        def _rep(m):
            key = m.group(1).strip()
            val = context['vars'].get(key)
            if val is None:
                val = context.get('last_output', '')
            return str(val) if val is not None else ''
        return re.sub(r'\{\{([^}]+)\}\}', _rep, str(template))

    def _eval_condition(self, expression, context):
        """Evaluate a condition expression; returns bool."""
        try:
            expr = self._substitute_vars(expression, context)

            # VALUE contains TEXT  (case-insensitive)
            m = re.match(r'^(.+?)\s+contains\s+[\'"]?(.+?)[\'"]?\s*$', expr, re.I)
            if m:
                return m.group(2).strip('"\'').lower() in m.group(1).lower()

            # VALUE equals TEXT  (case-insensitive)
            m = re.match(r'^(.+?)\s+(?:equals?|==)\s+[\'"]?(.+?)[\'"]?\s*$', expr, re.I)
            if m:
                return m.group(1).strip().lower() == m.group(2).strip('"\'').lower()

            # VALUE not empty
            m = re.match(r'^(.+?)\s+not\s+empty\s*$', expr, re.I)
            if m:
                return bool(m.group(1).strip())

            # VALUE starts with TEXT  (case-insensitive)
            m = re.match(r'^(.+?)\s+starts?\s+with\s+[\'"]?(.+?)[\'"]?\s*$', expr, re.I)
            if m:
                return m.group(1).lower().startswith(m.group(2).strip('"\'').lower())

            # VALUE ends with TEXT  (case-insensitive)
            m = re.match(r'^(.+?)\s+ends?\s+with\s+[\'"]?(.+?)[\'"]?\s*$', expr, re.I)
            if m:
                return m.group(1).lower().endswith(m.group(2).strip('"\'').lower())

            # Numeric comparisons: VALUE > N
            m = re.match(r'^(.+?)\s*(>=|<=|!=|>|<)\s*(.+)$', expr)
            if m:
                try:
                    lhs, op, rhs = float(m.group(1).strip()), m.group(2), float(m.group(3).strip())
                    return eval(f"{lhs} {op} {rhs}", {"__builtins__": {}})
                except (ValueError, TypeError):
                    pass

            # Fallback: python eval with no builtins
            return bool(eval(expr, {"__builtins__": {}}))
        except Exception:
            return True  # default true on error

    def _should_execute_node(self, nid, context, nodes, edges):
        """Return False if any incoming conditional edge's condition was not satisfied.

        Args:
            nid: ID of the node being considered for execution.
            context: Mutable run context dict (uses ``condition_results``).
            nodes: All workflow nodes.
            edges: All workflow edges.

        Returns:
            False if a parent conditional edge (``out_true``/``out_false``)
            blocks this path; True otherwise.
        """
        for edge in edges:
            src = edge.get('source') or edge.get('from')
            tgt = edge.get('target') or edge.get('to')
            if tgt != nid:
                continue
            src_port = edge.get('source_port', '')
            if not src_port or src_port == 'out':
                continue
            cond = context.get('condition_results', {}).get(src, True)
            if src_port == 'out_true' and not cond:
                return False
            if src_port == 'out_false' and cond:
                return False
        return True

    # ── Node executors ────────────────────────────────────────────────────────

    def _get_agent(self, agent_id):
        """Load an agent record (with parsed tools list) from the local hub_agents table.

        Args:
            agent_id: Agent ID to look up.

        Returns:
            Agent dict with ``tools`` parsed from JSON, or None if not found
            or on any DB error.
        """
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM hub_agents WHERE id=?", agent_id)
                cols = [d[0] for d in cur.description]
                row  = cur.fetchone()
            if not row:
                return None
            a = dict(zip(cols, row))
            a['tools'] = json.loads(a.pop('tools_json', '[]'))
            return a
        except Exception:
            return None

    def _exec_query_db(self, node, context):
        """Execute a SQL query against a named database connection.

        Resolves the connection by ``node['connection_name']`` via
        ``DatabaseConnectionManager`` (falling back to the first configured
        connection), substitutes ``{{var}}`` tokens in the query text, and
        dispatches to the matching driver (pyodbc/psycopg2/pymysql/sqlite3)
        based on the connection's ``type``.

        Args:
            node: Workflow node dict with ``connection_name`` and ``query``.
            context: Run context used for variable substitution.

        Returns:
            ``{"rows": [...], "count": N}`` on success, or
            ``{"error": "...", "rows": [], "count": 0}`` on failure.
        """
        try:
            from database_manager import DatabaseConnectionManager
            mgr = DatabaseConnectionManager()
            connections = mgr.load_connections()

            conn_name = node.get('connection_name', '')
            query = self._substitute_vars(node.get('query', ''), context)

            if not query.strip():
                return {"error": "No query specified", "rows": [], "count": 0}

            cfg = next((c for c in connections if c['name'] == conn_name), None)
            if not cfg and connections:
                cfg = connections[0]
            if not cfg:
                return {"error": "No database connection configured", "rows": [], "count": 0}

            db_type = cfg.get('type', '').lower()
            server   = cfg.get('server', '')
            database = cfg.get('database', '')
            username = cfg.get('username', '')
            password = cfg.get('password', '')
            port     = cfg.get('port', '')

            rows = []

            if any(t in db_type for t in ('sqlserver', 'mssql', 'sql server')):
                import pyodbc
                cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                      f"SERVER={server};DATABASE={database};"
                      f"UID={username};PWD={password};")
                with pyodbc.connect(cs, timeout=30) as c:
                    cur = c.cursor(); cur.execute(query)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = [dict(zip(cols, [str(v) if v is not None else None for v in r]))
                                for r in cur.fetchall()]

            elif any(t in db_type for t in ('postgres', 'postgresql')):
                import psycopg2
                with psycopg2.connect(host=server, dbname=database, user=username,
                                      password=password, port=int(port or 5432)) as c:
                    cur = c.cursor(); cur.execute(query)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = [dict(zip(cols, [str(v) if v is not None else None for v in r]))
                                for r in cur.fetchall()]

            elif 'mysql' in db_type:
                import pymysql
                with pymysql.connect(host=server, database=database, user=username,
                                     password=password, port=int(port or 3306)) as c:
                    cur = c.cursor(); cur.execute(query)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = [dict(zip(cols, [str(v) if v is not None else None for v in r]))
                                for r in cur.fetchall()]

            elif 'sqlite' in db_type:
                import sqlite3
                with sqlite3.connect(database) as c:
                    cur = c.cursor(); cur.execute(query)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            else:
                return {"error": f"Unsupported database type: {db_type}", "rows": [], "count": 0}

            return {"rows": rows, "count": len(rows)}

        except Exception as e:
            return {"error": str(e), "rows": [], "count": 0}

    def _exec_http_request(self, node, context):
        """Execute an HTTP request and return the response.

        Substitutes ``{{var}}`` tokens in ``url``, ``headers``, and ``body``
        before sending. ``headers``/``body`` are parsed as JSON when possible;
        otherwise the body is sent as a raw string.

        Args:
            node: Workflow node dict with ``url``, ``method``, ``headers``,
                ``body``.
            context: Run context used for variable substitution.

        Returns:
            ``{"status": int, "body": ...}`` (body parsed as JSON when
            possible, else truncated text), or ``{"error": "..."}``.
        """
        try:
            import requests as _req
            url     = self._substitute_vars(node.get('url', ''), context)
            method  = node.get('method', 'GET').upper()
            hdrs_s  = self._substitute_vars(node.get('headers', ''), context)
            body_s  = self._substitute_vars(node.get('body', ''), context)

            headers = {}
            if hdrs_s.strip():
                try:
                    headers = json.loads(hdrs_s)
                except Exception:
                    pass

            json_body = None
            data_body = None
            if body_s.strip():
                try:
                    json_body = json.loads(body_s)
                except Exception:
                    data_body = body_s

            resp = _req.request(method, url, headers=headers,
                                json=json_body, data=data_body, timeout=30)
            try:
                return {"status": resp.status_code, "body": resp.json()}
            except Exception:
                return {"status": resp.status_code, "body": resp.text[:5000]}

        except Exception as e:
            return {"error": str(e)}

    # ── Sequential runner ─────────────────────────────────────────────────────

    @staticmethod
    def _all_parents_skipped(nid, edges, skipped):
        """True when every parent edge of ``nid`` comes from an already-skipped node.

        Used to cascade-skip nodes whose entire upstream path was skipped by a
        conditional branch, so they don't run with stale/missing context.
        Nodes with no parents (start nodes) are never considered all-skipped.

        Args:
            nid: Node ID being checked.
            edges: All workflow edges.
            skipped: Set of node IDs already determined to be skipped.

        Returns:
            True if ``nid`` has at least one parent and all of them are in
            ``skipped``; False otherwise.
        """
        parents = [e.get('source') or e.get('from')
                   for e in edges
                   if (e.get('target') or e.get('to')) == nid]
        return bool(parents) and all(p in skipped for p in parents)

    def _run_sequential(self, nodes, edges, input_data):
        """Execute workflow nodes one at a time in topological order.

        Walks nodes in the order returned by ``_topo_sort``, skipping nodes
        whose conditional branch was not taken or whose parents were all
        skipped, and dispatches each node by its ``type`` (trigger, agent,
        query_db, condition, transform, http_request, set_variable, delay,
        output) — updating the shared ``context`` (``last_output``, ``vars``,
        ``outputs``, ``condition_results``) as it goes.

        Args:
            nodes: All workflow nodes.
            edges: All workflow edges.
            input_data: Trigger input value for the first node.

        Yields:
            JSON-encoded (NDJSON) strings: ``node_skipped``, ``node_start``,
            per-type result events (e.g. ``query_result``), ``node_complete``
            for each node, followed by one ``wf_complete`` at the end.
        """
        from core.orchestrator.engine import Orchestrator
        order   = self._topo_sort(nodes, edges)
        context = {
            'last_output':       input_data,
            'outputs':           {},
            'vars':              {'trigger_input': input_data},
            'condition_results': {},
        }
        skipped = set()

        for nid in order:
            node  = next((n for n in nodes if n['id'] == nid), None)
            if not node:
                continue
            ntype = node.get('type', 'agent')

            if not self._should_execute_node(nid, context, nodes, edges):
                skipped.add(nid)
                yield json.dumps({"type": "node_skipped", "node_id": nid}) + '\n'
                continue

            if self._all_parents_skipped(nid, edges, skipped):
                skipped.add(nid)
                yield json.dumps({"type": "node_skipped", "node_id": nid}) + '\n'
                continue

            yield json.dumps({"type": "node_start", "node_id": nid,
                               "node_type": ntype, "name": node.get('label', nid)}) + '\n'

            output_var = node.get('output_var', '').strip()

            # ── trigger ───────────────────────────────────────────────────
            if ntype == 'trigger':
                val = input_data
                context['last_output'] = val
                if output_var:
                    context['vars'][output_var] = val
                context['outputs'][nid] = val

            # ── agent ─────────────────────────────────────────────────────
            elif ntype == 'agent' and node.get('agent_id'):
                agent = self._get_agent(node['agent_id'])
                if agent:
                    question = self._substitute_vars(
                        node.get('question', '') or context['last_output'], context)
                    orch = Orchestrator(self.api_key)
                    for chunk in orch.run(question, agent):
                        try:
                            d = json.loads(chunk.strip())
                            d['node_id'] = nid
                            if d.get('type') == 'final':
                                val = d.get('content', '')
                                context['last_output'] = val
                                context['outputs'][nid] = val
                                if output_var:
                                    context['vars'][output_var] = val
                            yield json.dumps(d) + '\n'
                        except Exception:
                            yield chunk

            # ── query_db ─────────────────────────────────────────────────
            elif ntype == 'query_db':
                result = self._exec_query_db(node, context)
                val    = json.dumps(result, ensure_ascii=False)
                context['last_output'] = val
                context['outputs'][nid] = val
                if output_var:
                    context['vars'][output_var] = val
                yield json.dumps({"type": "query_result", "node_id": nid,
                                  "rows": result.get('rows', [])[:50],
                                  "count": result.get('count', 0),
                                  "error": result.get('error')}) + '\n'

            # ── condition ─────────────────────────────────────────────────
            elif ntype == 'condition':
                expression = node.get('expression', node.get('condition', ''))
                result     = self._eval_condition(expression, context)
                context['condition_results'][nid] = result
                yield json.dumps({"type": "condition_result", "node_id": nid,
                                  "result": result}) + '\n'

            # ── transform ─────────────────────────────────────────────────
            elif ntype == 'transform':
                val = self._substitute_vars(
                    node.get('expression', '{{trigger_input}}'), context)
                context['last_output'] = val
                context['outputs'][nid] = val
                if output_var:
                    context['vars'][output_var] = val

            # ── http_request ──────────────────────────────────────────────
            elif ntype == 'http_request':
                result = self._exec_http_request(node, context)
                val    = json.dumps(result, ensure_ascii=False)
                context['last_output'] = val
                context['outputs'][nid] = val
                if output_var:
                    context['vars'][output_var] = val
                yield json.dumps({"type": "http_result", "node_id": nid,
                                  "status": result.get('status'),
                                  "error": result.get('error')}) + '\n'

            # ── set_variable ──────────────────────────────────────────────
            elif ntype == 'set_variable':
                var_name = node.get('var_name', '').strip()
                val      = self._substitute_vars(node.get('value', ''), context)
                if var_name:
                    context['vars'][var_name] = val
                context['last_output'] = val
                context['outputs'][nid] = val

            # ── delay ─────────────────────────────────────────────────────
            elif ntype == 'delay':
                time.sleep(min(node.get('delay_seconds', 1), 5))

            # ── output ────────────────────────────────────────────────────
            elif ntype == 'output':
                dv  = node.get('display_var', '').strip()
                val = context['vars'].get(dv, context['last_output']) if dv else context['last_output']
                context['last_output'] = val
                context['outputs'][nid] = val

            yield json.dumps({"type": "node_complete", "node_id": nid,
                              "output_preview": str(context['outputs'].get(nid, ''))[:300]}) + '\n'

        yield json.dumps({
            "type":         "wf_complete",
            "final_output": context.get('last_output', ''),
            "vars":         {k: str(v)[:500] for k, v in context.get('vars', {}).items()},
        }) + '\n'

    # ── Parallel runner ───────────────────────────────────────────────────────

    def _run_parallel(self, nodes, input_data):
        """Run every agent node concurrently against the same input and combine results.

        Note: despite the name, each agent node is currently run synchronously
        in sequence (not via threads/async) — "parallel" here refers to the
        workflow's fan-out structure (no inter-node dependencies), not to
        concurrent execution.

        Args:
            nodes: All workflow nodes; only ``type == 'agent'`` nodes run.
            input_data: Input value passed identically to every agent node.

        Yields:
            JSON-encoded (NDJSON) ``node_complete`` events per agent node,
            then one ``wf_complete`` with all results concatenated.
        """
        from core.orchestrator.engine import Orchestrator
        results = []
        for node in nodes:
            if node.get('type') == 'agent' and node.get('agent_id'):
                agent = self._get_agent(node['agent_id'])
                if agent:
                    result = Orchestrator(self.api_key).run_simple(input_data, agent)
                    results.append({'node_id': node['id'], 'result': result.get('content', '')})
                    yield json.dumps({"type": "node_complete", "node_id": node['id'],
                                      "result": result.get('content', '')}) + '\n'
        combined = '\n\n'.join([f"**{r['node_id']}**: {r['result']}" for r in results])
        yield json.dumps({"type": "wf_complete", "final_output": combined}) + '\n'

    # ── Topological sort (Kahn's algorithm) ───────────────────────────────────

    def _topo_sort(self, nodes, edges):
        """Order nodes via Kahn's algorithm so each node follows its dependencies.

        Args:
            nodes: All workflow nodes.
            edges: All workflow edges (``source``/``from`` -> ``target``/``to``).

        Returns:
            List of node IDs in topological order. If there are no edges,
            returns nodes in their original order. Nodes involved in a cycle
            are silently omitted (Kahn's algorithm only emits nodes once
            their in-degree reaches zero).
        """
        if not edges:
            return [n['id'] for n in nodes]
        in_deg = {n['id']: 0 for n in nodes}
        graph  = {n['id']: [] for n in nodes}
        for e in edges:
            src = e.get('source') or e.get('from')
            tgt = e.get('target') or e.get('to')
            if src and tgt and src in graph:
                graph[src].append(tgt)
                in_deg[tgt] = in_deg.get(tgt, 0) + 1
        queue  = [nid for nid, d in in_deg.items() if d == 0]
        result = []
        while queue:
            n = queue.pop(0)
            result.append(n)
            for nb in graph.get(n, []):
                in_deg[nb] -= 1
                if not in_deg[nb]:
                    queue.append(nb)
        return result
