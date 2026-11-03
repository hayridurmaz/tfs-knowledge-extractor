"""Command line: kg schema | kg ingest | kg serve | kg stats."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from kg.config import Settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kg", description="Team code knowledge graph")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="create Neo4j constraints and indexes")
    ing = sub.add_parser("ingest", help="fetch repos, parse, build and load the code graph")
    ing.add_argument("--local-path", type=Path, help="index sub-folders of this directory instead of ADO mirrors")
    ing.add_argument("--skip-fetch", action="store_true", help="use existing mirrors without fetching")
    ing.add_argument("--repos", help="comma-separated repo names to fetch (the graph is still built from all mirrors)")
    ing.add_argument("--dry-run", action="store_true", help="build the graph and print stats, do not write Neo4j")
    sub.add_parser("serve", help="run the MCP server (streamable HTTP)")
    sub.add_parser("stats", help="print node/relationship counts from Neo4j")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("neo4j", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = Settings()

    if args.cmd == "schema":
        from kg.store import Store
        st = Store(s)
        st.init_schema()
        st.close()
        print("schema ok")
        return 0

    if args.cmd == "ingest":
        return _ingest(s, args)

    if args.cmd == "serve":
        from kg.mcp_server import serve
        serve(s)
        return 0

    if args.cmd == "stats":
        from kg.store import Store
        st = Store(s)
        labels = st.run("MATCH (n:Node) UNWIND labels(n) AS l WITH l WHERE l <> 'Node' "
                        "RETURN l, count(*) AS c ORDER BY c DESC")
        rels = st.run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC")
        state = st.run("MATCH (s:IngestState) RETURN s")
        st.close()
        print(json.dumps({"labels": {r["l"]: r["c"] for r in labels}, "relationships": {r["t"]: r["c"] for r in rels},
                          "ingest": [dict(r["s"]) for r in state]}, indent=2, default=str))
        return 0
    return 1


def _ingest(s: Settings, args) -> int:
    from kg.pipeline import ParseCache, build, fetch_mirrors, new_run_id, snapshots

    log = logging.getLogger("kg.ingest")
    t0 = time.time()
    failed: list[str] = []
    if not args.local_path and not args.skip_fetch:
        only = set(args.repos.split(",")) if args.repos else None
        failed = fetch_mirrors(s, only)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    snaps = snapshots(s, args.local_path)
    if not snaps:
        log.error("no repositories found (mirrors dir: %s)", s.mirrors_dir)
        return 2
    graph = build(snaps, ParseCache(s.cache_path))
    if args.dry_run:
        from collections import Counter
        print(json.dumps({"nodes": dict(Counter(n.label for n in graph.nodes.values())),
                          "edges": dict(Counter(e.type for e in graph.edges.values()))}, indent=2))
        return 0
    from kg.store import Store
    st = Store(s)
    try:
        st.init_schema()
        stats = st.write_graph(graph, new_run_id(), [x.name for x in snaps], full=True)
    finally:
        st.close()
    log.info("ingest finished in %.1fs: %s; fetch failures: %s", time.time() - t0, stats, failed or "none")
    # A failed fetch keeps the previous mirror state in the graph, but the CronJob should still be flagged.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
