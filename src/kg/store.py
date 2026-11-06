"""Neo4j persistence: schema, batched idempotent upserts, and sweeping of stale code facts."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict

from neo4j import Driver, GraphDatabase

from kg.config import Settings
from kg.graph import OPTIONAL_LABELS, Graph

log = logging.getLogger(__name__)
BATCH = 2000
_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA = [
    "CREATE CONSTRAINT node_id IF NOT EXISTS FOR (n:Node) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX node_name IF NOT EXISTS FOR (n:Node) ON (n.name)",
    "CREATE INDEX node_fqn IF NOT EXISTS FOR (n:Node) ON (n.fqn)",
    "CREATE INDEX node_repo IF NOT EXISTS FOR (n:Node) ON (n.repo)",
    "CREATE INDEX node_path IF NOT EXISTS FOR (n:Node) ON (n.path)",
    "CREATE INDEX endpoint_norm IF NOT EXISTS FOR (n:Endpoint) ON (n.path_norm)",
    "CREATE FULLTEXT INDEX node_text IF NOT EXISTS FOR (n:Node) "
    "ON EACH [n.name, n.fqn, n.path, n.signature, n.doc, n.tokens, n.url, n.key]",
    "CREATE FULLTEXT INDEX node_code IF NOT EXISTS FOR (n:Method|Function|Class) ON EACH [n.code]",
]


# Range indexes (and the id constraint) reject values over ~8 KB ("Property value is too large to index").
# Some of these properties come from free text in source code (a huge string literal used as a URL, topic or
# route), so cap them here. Full-text indexes have no such limit, and `url`/`code` stay complete.
RANGE_INDEXED = ("name", "fqn", "repo", "path", "path_norm")
MAX_INDEXED_BYTES = 4000
MAX_ID_BYTES = 1000


def _cap(value: str, limit: int) -> str:
    b = value.encode()
    return value if len(b) <= limit else b[:limit].decode(errors="ignore") + "…"


def safe_id(node_id: str) -> str:
    """Deterministic short form of an over-long id, so reruns MERGE onto the same node."""
    if len(node_id.encode()) <= MAX_ID_BYTES:
        return node_id
    return _cap(node_id, 200) + "#" + hashlib.sha1(node_id.encode()).hexdigest()


def safe_props(props: dict) -> dict:
    out = dict(props)
    for k in RANGE_INDEXED:
        v = out.get(k)
        if isinstance(v, str) and len(v.encode()) > MAX_INDEXED_BYTES:
            out[k] = _cap(v, MAX_INDEXED_BYTES)
    return out


def driver(settings: Settings) -> Driver:
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def _label(s: str) -> str:
    if not _SAFE.match(s):
        raise ValueError(f"unsafe label/type: {s!r}")
    return s


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.driver = driver(settings)
        self.db = settings.neo4j_database

    def close(self) -> None:
        self.driver.close()

    def run(self, q: str, **params):
        with self.driver.session(database=self.db) as s:
            return list(s.run(q, **params))

    def init_schema(self) -> None:
        for q in SCHEMA:
            self.run(q)
        self.run("CALL db.awaitIndexes(300)")

    # ------------------------------------------------------------ write
    def write_graph(self, g: Graph, run_id: str, repos: list[str], full: bool) -> dict:
        t0 = time.time()
        groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        capped = 0
        for n in g.nodes.values():
            labels = tuple(sorted({n.label, *n.extra_labels}))
            props = safe_props(n.props)
            nid = safe_id(n.id)
            if props != n.props or nid != n.id:
                capped += 1
                if capped <= 5:
                    log.warning("capped over-long indexed value on %s node %s", n.label, nid[:200])
            groups[labels].append({"id": nid, "p": props})
        if capped:
            log.warning("capped over-long indexed values on %d nodes", capped)
        remove = ":".join(OPTIONAL_LABELS)
        for labels, rows in groups.items():
            set_labels = ":".join(_label(x) for x in labels)
            # `+=` keeps properties added by later phases (e.g. LLM summaries) across nightly runs.
            q = (f"UNWIND $rows AS r MERGE (n:Node {{id: r.id}}) "
                 f"REMOVE n:{remove} SET n += r.p, n:{set_labels}, n.run = $run, n.src = 'code'")
            self._batched(q, rows, run=run_id)
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in g.edges.values():
            by_type[e.type].append({"s": safe_id(e.src), "t": safe_id(e.dst), "p": e.props})
        for typ, rows in by_type.items():
            q = (f"UNWIND $rows AS r MATCH (a:Node {{id: r.s}}) MATCH (b:Node {{id: r.t}}) "
                 f"MERGE (a)-[e:{_label(typ)}]->(b) SET e = r.p, e.run = $run, e.src = 'code'")
            self._batched(q, rows, run=run_id)
        t1 = time.time()
        deleted_edges = self._sweep(
            "MATCH ()-[e]->() WHERE e.src = 'code' AND e.run <> $run AND (e.repo IN $repos OR $full) "
            "WITH e LIMIT 20000 DELETE e RETURN count(*) AS c", run=run_id, repos=repos, full=full)
        deleted_nodes = self._sweep(
            "MATCH (n:Node) WHERE n.src = 'code' AND n.run <> $run "
            "AND (n.repo IN $repos OR ($full AND n.repo IS NULL)) "
            "WITH n LIMIT 10000 DETACH DELETE n RETURN count(*) AS c", run=run_id, repos=repos, full=full)
        self.run("MERGE (s:IngestState {id: 'code'}) SET s.run = $run, s.at = datetime(), "
                 "s.repos = $repos, s.nodes = $n, s.edges = $e",
                 run=run_id, repos=repos, n=len(g.nodes), e=len(g.edges))
        stats = {"nodes": len(g.nodes), "edges": len(g.edges), "deleted_nodes": deleted_nodes,
                 "deleted_edges": deleted_edges, "write_s": round(t1 - t0, 1), "sweep_s": round(time.time() - t1, 1)}
        log.info("neo4j write: %s", stats)
        return stats

    # ------------------------------------------------------------ history (phase 2)
    def write_history(self, g: Graph) -> dict:
        """Upsert history nodes, then edges. Unlike write_graph there is no run stamp or sweep: history only
        accumulates. Edges whose endpoint is not in the database (e.g. a method that no longer exists) are
        skipped by the MATCH; the File-level MODIFIED edge still records the change."""
        t0 = time.time()
        groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        for n in g.nodes.values():
            labels = tuple(sorted({n.label, *n.extra_labels}))
            groups[labels].append({"id": safe_id(n.id), "p": safe_props(n.props)})
        for labels, rows in groups.items():
            set_labels = ":".join(_label(x) for x in labels)
            self._batched(f"UNWIND $rows AS r MERGE (n:Node {{id: r.id}}) "
                          f"SET n += r.p, n:{set_labels}, n.src = 'history'", rows)
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in g.edges.values():
            by_type[e.type].append({"s": safe_id(e.src), "t": safe_id(e.dst), "p": e.props})
        written = 0
        for typ, rows in by_type.items():
            q = (f"UNWIND $rows AS r MATCH (a:Node {{id: r.s}}) MATCH (b:Node {{id: r.t}}) "
                 f"MERGE (a)-[e:{_label(typ)}]->(b) SET e += r.p, e.src = 'history' RETURN count(e) AS c")
            written += self._batched_count(q, rows)
        stats = {"history_nodes": len(g.nodes), "history_edges": len(g.edges), "history_edges_written": written,
                 "history_write_s": round(time.time() - t0, 1)}
        log.info("neo4j history write: %s", stats)
        return stats

    def history_state(self) -> dict:
        rows = self.run("MATCH (s:IngestState {id: 'history'}) RETURN s.state AS state")
        return json.loads(rows[0]["state"]) if rows and rows[0]["state"] else {}

    def save_history_state(self, state: dict) -> None:
        self.run("MERGE (s:IngestState {id: 'history'}) SET s.state = $state, s.at = datetime()",
                 state=json.dumps(state))

    def _batched_count(self, q: str, rows: list[dict], **params) -> int:
        total = 0
        with self.driver.session(database=self.db) as s:
            for i in range(0, len(rows), BATCH):
                total += s.execute_write(lambda tx, chunk: tx.run(q, rows=chunk, **params).single()["c"],
                                         rows[i:i + BATCH])
        return total

    def _batched(self, q: str, rows: list[dict], **params) -> None:
        with self.driver.session(database=self.db) as s:
            for i in range(0, len(rows), BATCH):
                s.execute_write(lambda tx, chunk: tx.run(q, rows=chunk, **params).consume(), rows[i:i + BATCH])

    def _sweep(self, q: str, **params) -> int:
        total = 0
        while True:
            c = self.run(q, **params)[0]["c"]
            total += c
            if c == 0:
                return total
