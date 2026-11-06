"""Read-side graph queries used by the MCP tools (kept separate so they can be tested without MCP)."""
from __future__ import annotations

import re
from typing import Any

import neo4j
from neo4j import unit_of_work
from neo4j.graph import Node, Path, Relationship

from kg.build import norm_path
from kg.graph import tokens

MAX_CODE = 8000
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
CALL_RELS = "CALLS|USES_HOOK|RENDERS"


def _lucene(q: str) -> str:
    terms = [t for t in re.split(r"\s+", q.strip()) if t]
    words = set(terms) | set(tokens(*terms).split())
    out = []
    for w in words:
        e = _LUCENE_SPECIAL.sub(r"\\\1", w)
        out.append(f"{e}^2" if w in terms else e)
        if len(w) >= 3 and w in terms:
            out.append(f"{e}*")
    return " OR ".join(out) or '""'


def brief(n: Node) -> dict:
    p = dict(n)
    out = {"id": p.get("id"), "labels": sorted(l for l in n.labels if l != "Node"), "name": p.get("name")}
    for k in ("repo", "path", "start_line", "signature", "fqn", "http_method", "url", "stereotype", "file"):
        if p.get(k) not in (None, ""):
            out["line" if k == "start_line" else k] = p[k]
    return out


def to_json(v: Any, code_chars: int = 2000) -> Any:
    if isinstance(v, Node):
        p = dict(v)
        p.pop("tokens", None)
        if isinstance(p.get("code"), str) and len(p["code"]) > code_chars:
            p["code"] = p["code"][:code_chars] + "\n…(truncated)"
        return {"labels": sorted(l for l in v.labels if l != "Node"), **p}
    if isinstance(v, Relationship):
        return {"type": v.type, "start": v.start_node["id"] if v.start_node else None,
                "end": v.end_node["id"] if v.end_node else None, **dict(v)}
    if isinstance(v, Path):
        return {"nodes": [brief(n) for n in v.nodes], "rels": [r.type for r in v.relationships]}
    if isinstance(v, (list, tuple)):
        return [to_json(x, code_chars) for x in v]
    if isinstance(v, dict):
        return {k: to_json(x, code_chars) for k, x in v.items()}
    if hasattr(v, "iso_format"):
        return v.iso_format()
    return v


class Queries:
    def __init__(self, driver: neo4j.Driver, database: str = "neo4j") -> None:
        self.driver = driver
        self.db = database

    def _read(self, text: str, /, **params) -> list[neo4j.Record]:
        @unit_of_work(timeout=20)
        def work(tx):
            return list(tx.run(text, params))

        with self.driver.session(database=self.db, default_access_mode=neo4j.READ_ACCESS) as s:
            return s.execute_read(work)

    # ------------------------------------------------------------ discovery
    def search(self, query: str, kinds: list[str] | None = None, repo: str | None = None,
               limit: int = 20, search_code: bool = False) -> list[dict]:
        limit = max(1, min(limit, 100))
        index = "node_code" if search_code else "node_text"
        # code search is a phrase query, so string literals / error messages match exactly
        q = '"' + _LUCENE_SPECIAL.sub(r"\\\1", query) + '"' if search_code else _lucene(query)
        rows = self._read(
            "CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score "
            "WHERE ($repo IS NULL OR node.repo = $repo) "
            "AND ($kinds IS NULL OR any(l IN labels(node) WHERE l IN $kinds)) "
            "RETURN node, score ORDER BY score DESC LIMIT $limit",
            index=index, q=q, repo=repo, kinds=kinds or None, limit=limit)
        return [{**brief(r["node"]), "score": round(r["score"], 2)} for r in rows]

    def find_symbol(self, name: str, kind: str | None = None, repo: str | None = None, limit: int = 25) -> list[dict]:
        base = ("MATCH (n:Node) WHERE {cond} AND ($kind IS NULL OR $kind IN labels(n)) "
                "AND ($repo IS NULL OR n.repo = $repo) RETURN n LIMIT $limit")
        params = dict(name=name, kind=kind, repo=repo, limit=limit)
        rows = self._read(base.format(cond="(n.id = $name OR n.name = $name OR n.fqn = $name)"), **params)
        if not rows:
            rows = self._read(base.format(
                cond="(n.fqn ENDS WITH '.' + $name OR toLower(n.name) = toLower($name))"), **params)
        return [brief(r["n"]) for r in rows]

    def get_node(self, id: str, include_code: bool = True) -> dict | None:
        rows = self._read("MATCH (n:Node {id: $id}) RETURN n", id=id)
        if not rows:
            return None
        n = rows[0]["n"]
        out = to_json(n, MAX_CODE if include_code else 0)
        if not include_code:
            out.pop("code", None)
        rels = self._read(
            "MATCH (n:Node {id: $id})-[r]-(m) "
            "RETURN type(r) AS t, startNode(r) = n AS out, count(*) AS c", id=id)
        out["relationships"] = {f"{'->' if r['out'] else '<-'}{r['t']}": r["c"] for r in rels}
        parent = self._read("MATCH (p:Node)-[:CONTAINS]->(n:Node {id: $id}) RETURN p", id=id)
        if parent:
            out["parent"] = [brief(r["p"]) for r in parent]
        if {"Class", "File", "Module", "Package"} & set(n.labels):
            members = self._read(
                "MATCH (n:Node {id: $id})-[:CONTAINS]->(m) RETURN m ORDER BY m.start_line, m.name LIMIT 300", id=id)
            out["members"] = [brief(r["m"]) for r in members]
        return out

    def neighbors(self, id: str, rel_types: list[str] | None = None, direction: str = "both",
                  limit: int = 50) -> list[dict]:
        rows = self._read(
            "MATCH (n:Node {id: $id})-[r]-(m) "
            "WHERE ($types IS NULL OR type(r) IN $types) "
            "AND ($dir = 'both' OR ($dir = 'out' AND startNode(r) = n) OR ($dir = 'in' AND endNode(r) = n)) "
            "RETURN type(r) AS type, startNode(r) = n AS outgoing, properties(r) AS props, m LIMIT $limit",
            id=id, types=rel_types or None, dir=direction, limit=max(1, min(limit, 300)))
        return [{"rel": r["type"], "direction": "out" if r["outgoing"] else "in",
                 **{k: v for k, v in r["props"].items() if k in ("confidence", "count", "via", "lines", "scope")},
                 "node": brief(r["m"])} for r in rows]

    # ------------------------------------------------------------ call graph
    def _callers_step(self, ids: list[str]) -> list[neo4j.Record]:
        return self._read(
            "UNWIND $ids AS id MATCH (x:Node {id: id}) "
            "OPTIONAL MATCH (x)-[:OVERRIDES*0..3]->(b) WITH DISTINCT x, b "
            f"MATCH (c)-[r:{CALL_RELS}]->(b) "
            "RETURN x.id AS target, c, type(r) AS rel, r.confidence AS conf, b.id <> x.id AS via_interface",
            ids=ids)

    def callers(self, id: str, depth: int = 2, limit: int = 100) -> list[dict]:
        return self._bfs(id, depth, limit, self._callers_step, "caller")

    def _callees_step(self, ids: list[str]) -> list[neo4j.Record]:
        return self._read(
            "UNWIND $ids AS id MATCH (x:Node {id: id}) "
            f"MATCH (x)-[r:{CALL_RELS}|CALLS_API|INSTANTIATES]->(c) "
            "OPTIONAL MATCH (impl)-[:OVERRIDES]->(c) "
            "RETURN x.id AS target, c, type(r) AS rel, r.confidence AS conf, collect(impl)[..10] AS impls",
            ids=ids)

    def callees(self, id: str, depth: int = 2, limit: int = 100) -> list[dict]:
        return self._bfs(id, depth, limit, self._callees_step, "callee")

    def _bfs(self, start: str, depth: int, limit: int, step, role: str) -> list[dict]:
        depth = max(1, min(depth, 5))
        seen, frontier, out = {start}, [start], []
        for level in range(1, depth + 1):
            if not frontier or len(out) >= limit:
                break
            nxt = []
            for r in step(frontier):
                # callees of an interface method also include its implementations (runtime dispatch)
                found = [(r["c"], False)] + [(i, True) for i in (r.get("impls") or [])]
                for node, is_impl in found:
                    nid = node["id"]
                    if nid in seen:
                        continue
                    seen.add(nid)
                    item = {**brief(node), "depth": level, "rel": "IMPLEMENTATION" if is_impl else r["rel"],
                            ("calls" if role == "caller" else "called_from"): r["target"]}
                    if r["conf"] is not None and not is_impl:
                        item["confidence"] = r["conf"]
                    if role == "caller" and r["via_interface"]:
                        item["via_interface"] = True
                    out.append(item)
                    nxt.append(nid)
                    if len(out) >= limit:
                        break
            frontier = nxt
        return out

    def impact(self, id: str, depth: int = 3) -> dict:
        start = self._read(
            "MATCH (n:Node {id: $id}) OPTIONAL MATCH (n)-[:CONTAINS*1..2]->(m) "
            "WHERE m:Method OR m:Function OR m:Class RETURN n, collect(DISTINCT m) AS members", id=id)
        if not start:
            return {"error": f"node not found: {id}"}
        roots = [id] + [m["id"] for m in start[0]["members"]]
        upstream: dict[str, dict] = {}
        for rid in roots[:50]:
            for item in self.callers(rid, depth=depth, limit=200):
                upstream.setdefault(item["id"], item)
        affected = roots + list(upstream)
        eps = self._read(
            "UNWIND $ids AS id MATCH (m:Node {id: id})-[:EXPOSES]->(e:Endpoint) RETURN DISTINCT e", ids=affected)
        ep_ids = [r["e"]["id"] for r in eps]
        consumers = self._read(
            "UNWIND $eps AS eid MATCH (c)-[k:CONSUMES]->(e:Endpoint {id: eid}) "
            "OPTIONAL MATCH (fn)-[:CALLS_API]->(c) "
            "RETURN e.id AS endpoint, coalesce(fn, c) AS consumer, k.confidence AS conf", eps=ep_ids)
        consumer_items = [{"endpoint": r["endpoint"], "confidence": r["conf"], **brief(r["consumer"])} for r in consumers]
        # walk up from consumers (other services / frontend components) to find what depends on them
        downstream_users: dict[str, dict] = {}
        for c in consumer_items[:30]:
            for item in self.callers(c["id"], depth=3, limit=100):
                downstream_users.setdefault(item["id"], item)
        comp_ids = [c["id"] for c in consumer_items] + list(downstream_users)
        routes = self._read(
            "UNWIND $ids AS id MATCH (r:Route)-[:ROUTES_TO]->(c:Node {id: id}) RETURN DISTINCT r", ids=comp_ids)
        topics = self._read(
            "UNWIND $ids AS id MATCH (m:Node {id: id})-[:PUBLISHES]->(t:Topic) "
            "OPTIONAL MATCH (s)-[:SUBSCRIBES]->(t) RETURN t.name AS topic, collect(DISTINCT s.id) AS subscribers",
            ids=affected)
        cap = 60
        return {
            "start": brief(start[0]["n"]),
            "upstream_callers": list(upstream.values())[:cap],
            "endpoints": [brief(r["e"]) for r in eps][:cap],
            "endpoint_consumers": consumer_items[:cap],
            "consumer_callers": list(downstream_users.values())[:cap],
            "routes": [brief(r["r"]) for r in routes][:cap],
            "topics": [{"topic": r["topic"], "subscribers": r["subscribers"]} for r in topics],
            "truncated": any(len(x) > cap for x in (upstream, eps, consumer_items, downstream_users)),
        }

    # ------------------------------------------------------------ service map
    def endpoints(self, path: str | None = None, repo: str | None = None, http_method: str | None = None,
                  limit: int = 50) -> list[dict]:
        pn = norm_path(path)[0] if path else None
        rows = self._read(
            "MATCH (e:Endpoint) WHERE ($repo IS NULL OR e.repo = $repo) "
            "AND ($m IS NULL OR e.http_method = $m OR e.http_method = 'ANY') "
            "AND ($p IS NULL OR e.path_norm CONTAINS $pn OR toLower(e.path) CONTAINS toLower($p)) "
            "OPTIONAL MATCH (h)-[:EXPOSES]->(e) "
            "OPTIONAL MATCH (c)-[k:CONSUMES]->(e) OPTIONAL MATCH (fn)-[:CALLS_API]->(c) "
            "WITH e, collect(DISTINCT h.id) AS handlers, "
            "collect(DISTINCT CASE WHEN c IS NULL THEN NULL ELSE "
            "{id: coalesce(fn.id, c.id), repo: c.repo, confidence: k.confidence} END) AS consumers "
            "RETURN e, handlers, consumers ORDER BY e.repo, e.path LIMIT $limit",
            repo=repo, m=http_method.upper() if http_method else None, p=path, pn=pn, limit=max(1, min(limit, 300)))
        return [{**brief(r["e"]), "path": r["e"]["path"], "handlers": r["handlers"],
                 "consumers": r["consumers"]} for r in rows]

    def list_repos(self) -> list[dict]:
        rows = self._read(
            "MATCH (r:Repository) "
            "OPTIONAL MATCH (r)-[:DEPENDS_ON_REPO]->(d) "
            "WITH r, collect(DISTINCT d.name) AS deps "
            "CALL (r) { MATCH (e:Endpoint {repo: r.name}) RETURN count(e) AS endpoints } "
            "CALL (r) { MATCH (c:Class {repo: r.name}) RETURN count(c) AS classes } "
            "CALL (r) { MATCH (f:Component {repo: r.name}) RETURN count(f) AS components } "
            "RETURN r, deps, endpoints, classes, components ORDER BY r.name")
        out = []
        for r in rows:
            svc = self._read("MATCH (e:Endpoint {repo: $n}) RETURN e.service_names AS s LIMIT 1", n=r["r"]["name"])
            out.append({"name": r["r"]["name"], "default_branch": r["r"].get("default_branch"),
                        "service_names": svc[0]["s"] if svc else None, "classes": r["classes"],
                        "endpoints": r["endpoints"], "components": r["components"], "depends_on_repos": r["deps"]})
        return out

    def repo_overview(self, repo: str) -> dict:
        counts = self._read(
            "MATCH (n:Node {repo: $repo}) UNWIND labels(n) AS l WITH l WHERE l <> 'Node' "
            "RETURN l, count(*) AS c ORDER BY c DESC", repo=repo)
        if not counts:
            return {"error": f"unknown repo: {repo}"}
        modules = self._read(
            "MATCH (:Repository {name: $repo})-[:CONTAINS]->(m:Module) "
            "OPTIONAL MATCH (m)-[d:DEPENDS_ON]->(a) "
            "RETURN m, collect({dep: a.id, scope: d.scope, version: d.version})[..80] AS deps", repo=repo)
        pkgs = self._read(
            "MATCH (p:Package {repo: $repo})-[:CONTAINS]->(c) RETURN p.name AS name, count(c) AS classes "
            "ORDER BY classes DESC LIMIT 40", repo=repo)
        rdeps = self._read(
            "MATCH (r:Repository {name: $repo}) OPTIONAL MATCH (r)-[:DEPENDS_ON_REPO]->(o) "
            "OPTIONAL MATCH (i)-[:DEPENDS_ON_REPO]->(r) "
            "RETURN collect(DISTINCT o.name) AS out, collect(DISTINCT i.name) AS inc", repo=repo)
        calls_out = self._read(
            "MATCH (c:Node {repo: $repo})-[:CONSUMES]->(e:Endpoint) WHERE e.repo <> $repo "
            "RETURN e.repo AS repo, count(*) AS calls ORDER BY calls DESC", repo=repo)
        calls_in = self._read(
            "MATCH (c:Node)-[:CONSUMES]->(e:Endpoint {repo: $repo}) WHERE c.repo <> $repo "
            "RETURN c.repo AS repo, count(*) AS calls ORDER BY calls DESC", repo=repo)
        return {
            "repo": repo,
            "counts": {r["l"]: r["c"] for r in counts},
            "modules": [{**brief(r["m"]), "artifact": r["m"].get("artifact"), "build": r["m"].get("build"),
                         "dependencies": [d for d in r["deps"] if d["dep"]]} for r in modules],
            "top_packages": [{"name": r["name"], "classes": r["classes"]} for r in pkgs],
            "depends_on_repos": rdeps[0]["out"] if rdeps else [],
            "depended_on_by_repos": rdeps[0]["inc"] if rdeps else [],
            "http_calls_to_repos": [{"repo": r["repo"], "calls": r["calls"]} for r in calls_out],
            "http_calls_from_repos": [{"repo": r["repo"], "calls": r["calls"]} for r in calls_in],
        }

    # ------------------------------------------------------------ history (phase 2)
    def history(self, id: str, limit: int = 20, work_item_types: list[str] | None = None) -> list[dict] | dict:
        limit = max(1, min(limit, 100))
        fetch = min(limit * 5, 500) if work_item_types else limit
        node = self._read("MATCH (n:Node {id: $id}) RETURN n", id=id)
        if not node:
            return {"error": f"node not found: {id}"}
        tail = (
            "WITH c, collect(DISTINCT t.id) AS touched, sum(coalesce(mod.added, 0)) AS added, "
            "sum(coalesce(mod.deleted, 0)) AS deleted ORDER BY c.date DESC LIMIT $limit "
            "RETURN c, touched, added, deleted, "
            "COLLECT { MATCH (pr:PullRequest)-[:MERGED_AS]->(c) RETURN pr } AS prs, "
            "COLLECT { MATCH (c)-[r:RESOLVES]->(w:WorkItem) RETURN {w: w, via: r.via, conf: r.confidence} } + "
            "COLLECT { MATCH (pr:PullRequest)-[:MERGED_AS]->(c) MATCH (pr)-[r:RESOLVES]->(w:WorkItem) "
            "          RETURN {w: w, via: 'pr-' + r.via, conf: r.confidence} } AS wis")
        if "Repository" in node[0]["n"].labels:
            rows = self._read("MATCH (c:Node {repo: $repo}) WHERE c:Commit "
                              "OPTIONAL MATCH (c)-[mod:MODIFIED]->(t:File) " + tail,
                              repo=node[0]["n"].get("name"), limit=fetch)
        else:
            rows = self._read(
                "MATCH (n:Node {id: $id}) OPTIONAL MATCH (n)-[:CONTAINS]->(m) WHERE m:Method OR m:Function "
                "WITH n, collect(m) AS members WITH [n] + members AS targets UNWIND targets AS t "
                "MATCH (c:Commit)-[mod:MODIFIED]->(t) " + tail, id=id, limit=fetch)
        out = []
        types = {t.lower() for t in work_item_types or []}
        for r in rows:
            wis: dict[str, dict] = {}
            for x in r["wis"]:
                w = x["w"]
                cur = wis.get(w["id"])
                if cur is None or (x["conf"] or 0) > cur["confidence"]:
                    wis[w["id"]] = {"id": w["id"], "number": w.get("number"), "type": w.get("type"),
                                    "title": w.get("title"), "state": w.get("state"), "via": x["via"],
                                    "confidence": x["conf"] or 0}
            if types and not any((w["type"] or "").lower() in types for w in wis.values()):
                continue
            c = r["c"]
            out.append({
                "commit": c.get("short"), "sha": c.get("sha"), "id": c.get("id"), "repo": c.get("repo"),
                "date": c.get("date"), "author": c.get("author"), "message": (c.get("message") or "")[:500],
                "added": r["added"], "deleted": r["deleted"], "touched": r["touched"][:20],
                "pull_requests": [{"id": p.get("id"), "number": p.get("number"), "title": p.get("title")}
                                  for p in r["prs"]],
                "work_items": sorted(wis.values(), key=lambda w: -w["confidence"]),
            })
            if len(out) >= limit:
                break
        return out

    def work_item(self, id: str | int) -> dict:
        m = re.search(r"(\d+)\s*$", str(id))
        if not m:
            return {"error": f"not a work item id: {id}"}
        wid = f"wi:{int(m.group(1))}"
        rows = self._read("MATCH (w:WorkItem {id: $id}) RETURN w", id=wid)
        if not rows:
            return {"error": f"work item not found in the graph: {wid} (only items changed in the history window "
                             f"or referenced from commits/PRs are loaded)"}
        out = to_json(rows[0]["w"], 0)
        out.pop("doc", None)
        rel = self._read(
            "MATCH (w:WorkItem {id: $id}) "
            "RETURN COLLECT { MATCH (w)-[:CHILD_OF]->(p) RETURN p } AS parents, "
            "COLLECT { MATCH (ch)-[:CHILD_OF]->(w) RETURN ch } AS children, "
            "COLLECT { MATCH (pr:PullRequest)-[r:RESOLVES]->(w) RETURN {pr: pr, via: r.via} } AS prs", id=wid)[0]
        wi_brief = lambda n: {"id": n.get("id"), "type": n.get("type"), "title": n.get("title"),  # noqa: E731
                              "state": n.get("state")}
        out["parent"] = [wi_brief(p) for p in rel["parents"]]
        out["children"] = [wi_brief(c) for c in rel["children"]][:100]
        out["pull_requests"] = [{"id": x["pr"].get("id"), "number": x["pr"].get("number"), "repo": x["pr"].get("repo"),
                                 "title": x["pr"].get("title"), "closed": x["pr"].get("closed"), "via": x["via"]}
                                for x in rel["prs"]]
        commits_q = (
            "CALL { MATCH (c:Commit)-[:RESOLVES]->(:WorkItem {id: $id}) RETURN c "
            "UNION MATCH (pr:PullRequest)-[:RESOLVES]->(:WorkItem {id: $id}) "
            "MATCH (pr)-[:MERGED_AS]->(c:Commit) RETURN c } ")
        commits = self._read(commits_q + "RETURN c ORDER BY c.date DESC LIMIT 100", id=wid)
        out["commits"] = [{"commit": r["c"].get("short"), "id": r["c"].get("id"), "repo": r["c"].get("repo"),
                           "date": r["c"].get("date"), "author": r["c"].get("author"), "message": r["c"].get("name")}
                          for r in commits]
        touched = self._read(
            commits_q + "WITH c MATCH (c)-[:MODIFIED]->(t) WHERE t:Method OR t:Function OR t:Class OR t:File "
            "RETURN t, count(DISTINCT c) AS commits ORDER BY commits DESC LIMIT 200", id=wid)
        out["touched_code"] = [{**brief(r["t"]), "commits": r["commits"]} for r in touched
                               if "File" not in r["t"].labels][:50]
        out["touched_files"] = [{**brief(r["t"]), "commits": r["commits"]} for r in touched
                                if "File" in r["t"].labels][:50]
        return out

    def cypher(self, query: str, params: dict | None = None, limit: int = 200) -> list[Any]:
        limit = max(1, min(limit, 1000))
        with self.driver.session(database=self.db, default_access_mode=neo4j.READ_ACCESS) as s:
            @unit_of_work(timeout=15)
            def work(tx):
                res = tx.run(query, **(params or {}))
                out = []
                for i, rec in enumerate(res):
                    if i >= limit:
                        break
                    out.append({k: to_json(v) for k, v in rec.items()})
                return out
            return s.execute_read(work)
