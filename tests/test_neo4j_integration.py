"""Loads the fixture graph into a real Neo4j and exercises the MCP query layer.

Needs a running Neo4j: `docker compose up -d neo4j` and KG_NEO4J_URI/KG_NEO4J_PASSWORD set.
WARNING: wipes the target database.
"""
import os

import pytest

from kg.config import Settings

pytestmark = pytest.mark.neo4j

if not os.environ.get("KG_NEO4J_URI"):
    pytest.skip("KG_NEO4J_URI not set (integration test needs Neo4j)", allow_module_level=True)


@pytest.fixture(scope="module")
def q(graph):
    from kg.queries import Queries
    from kg.store import Store

    s = Settings()
    st = Store(s)
    st.run("MATCH (n) DETACH DELETE n")
    st.init_schema()
    # write a stale node first to prove the sweep removes it
    st.run("CREATE (:Node:Class {id: 'java:order-service:old.Gone', repo: 'order-service', src: 'code', run: 'old'})")
    stats = st.write_graph(graph, "run-1", sorted({n.props["repo"] for n in graph.nodes.values()
                                                   if n.label == "Repository"}), full=True)
    assert stats["deleted_nodes"] == 1
    # idempotent: a second run with the same graph neither duplicates nor deletes
    stats2 = st.write_graph(graph, "run-2", ["common-lib", "customer-service", "order-service", "web-app"], full=True)
    assert stats2["deleted_nodes"] == 0 and stats2["deleted_edges"] == 0
    assert st.run("MATCH (n:Node) RETURN count(n) AS c")[0]["c"] == len(graph.nodes)
    assert st.run("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"] == len(graph.edges)
    yield Queries(st.driver, s.neo4j_database)
    st.close()


O = "java:order-service:com.acme.order"


def test_search_and_find(q):
    hits = q.search("order service", kinds=["Class"])
    assert hits[0]["id"] in (f"{O}.service.OrderService", f"{O}.service.OrderServiceImpl")
    assert q.search("Customer not found", search_code=True)[0]["id"] == f"{O}.service.OrderServiceImpl#place(Order)"
    assert [x["id"] for x in q.find_symbol("OrderController")] == [f"{O}.api.OrderController"]
    assert q.find_symbol("api.OrderController")[0]["id"] == f"{O}.api.OrderController"  # FQN suffix


def test_get_node(q):
    n = q.get_node(f"{O}.api.OrderController")
    assert "@RestController" in n["annotations"]
    assert any(m["name"] == "get" for m in n["members"])
    assert n["relationships"]["->INJECTS"] == 1
    m = q.get_node(f"{O}.service.OrderServiceImpl#place(Order)")
    assert "customerClient.exists" in m["code"]


def test_callers_through_interface(q):
    callers = q.callers(f"{O}.service.OrderServiceImpl#place(Order)")
    c = next(x for x in callers if x["id"] == f"{O}.api.OrderController#create(Order)")
    assert c["via_interface"] is True


def test_callees_include_implementations(q):
    ids = {x["id"]: x["rel"] for x in q.callees(f"{O}.api.OrderController#create(Order)", depth=1)}
    assert ids[f"{O}.service.OrderService#place(Order)"] == "CALLS"
    assert ids[f"{O}.service.OrderServiceImpl#place(Order)"] == "IMPLEMENTATION"


def test_impact_reaches_frontend_route(q):
    imp = q.impact(f"{O}.service.OrderServiceImpl#find(Long)")
    assert "ep:order-service:GET:/api/orders/{id}" in {e["id"] for e in imp["endpoints"]}
    assert "ts:web-app:src/api/client.ts#getOrder" in {c["id"] for c in imp["endpoint_consumers"]}
    users = {c["id"] for c in imp["consumer_callers"]}
    assert "ts:web-app:src/pages/OrderPage.tsx#OrderPage" in users
    assert "route:web-app:/orders/:id" in {r["id"] for r in imp["routes"]}


def test_endpoints_and_repos(q):
    eps = q.endpoints(path="/customers/:id/exists")
    assert eps[0]["consumers"][0]["id"] == f"{O}.client.CustomerClient#exists(Long)"
    repos = {r["name"]: r for r in q.list_repos()}
    assert repos["order-service"]["depends_on_repos"] == ["common-lib"]
    ov = q.repo_overview("order-service")
    assert ov["http_calls_to_repos"] == [{"repo": "customer-service", "calls": 1}]
    assert ov["http_calls_from_repos"] == [{"repo": "web-app", "calls": 3}]


def test_cypher_is_read_only(q):
    assert q.cypher("MATCH (r:Repository) RETURN r.name AS n ORDER BY n")[0] == {"n": "common-lib"}
    with pytest.raises(Exception):
        q.cypher("CREATE (:Hacked)")


def test_mcp_tools_registered():
    from kg.mcp_server import create_server
    import anyio

    server = create_server(Settings())
    names = {t.name for t in anyio.run(server.list_tools)}
    assert {"search", "find_symbol", "get_node", "callers", "callees", "impact", "endpoints", "cypher",
            "history", "work_item"} <= names


def test_oversized_indexed_values_are_written(q):
    """A huge string literal used as URL/topic must not fail the load (range index key limit ~8 KB)."""
    from kg.graph import Graph
    from kg.store import Store

    huge = "/api/" + "x" * 20000
    g = Graph()
    g.node("repo:huge-repo", "Repository", name="huge-repo", repo="huge-repo")
    g.node(f"topic:{huge}", "Topic", name=huge, repo="huge-repo")
    g.node("api:huge-repo:A.java:1", "ApiCall", name=f"GET {huge}", url=huge, repo="huge-repo", path="A.java")
    g.edge("PUBLISHES", "api:huge-repo:A.java:1", f"topic:{huge}")
    st = Store(Settings())
    try:
        st.write_graph(g, "run-huge", ["huge-repo"], full=False)
        st.write_graph(g, "run-huge-2", ["huge-repo"], full=False)  # rerun merges onto the same nodes
        rows = st.run("MATCH (a:ApiCall {repo: 'huge-repo'})-[:PUBLISHES]->(t:Topic) RETURN a.url AS url")
        assert len(rows) == 1 and rows[0]["url"] == huge
    finally:
        st.run("MATCH (n:Node {repo: 'huge-repo'}) DETACH DELETE n")
        st.close()


def test_history_written_and_queried(q, tmp_path):
    """Commits from a real git repo + a work item, linked onto the fixture code graph."""
    from conftest import FIND, PLACE, make_history_repo

    from kg.history import HistoryGraph, commit_id, commits_with_spans
    from kg.pipeline import ParseCache
    from kg.store import Store

    mirror, shas = make_history_repo(tmp_path)
    h = HistoryGraph()
    commits_with_spans(mirror, ParseCache(None), h, since=None, exclude=None)
    h.add_pull_request("order-service", {"pullRequestId": 77, "title": "Better error in find",
                                         "sourceRefName": "refs/heads/feature/5555-find",
                                         "creationDate": "2025-03-01T10:00:00Z",
                                         "lastMergeCommit": {"commitId": shas["merge"]}})
    for n, typ, title in ((4321, "Bug", "Total is wrong"), (5555, "Product Backlog Item", "Clear errors")):
        h.add_work_item({"id": n, "fields": {"System.WorkItemType": typ, "System.Title": title}}, {})
    st = Store(Settings())
    try:
        st.write_history(h.g)
        st.write_history(h.g)  # idempotent
        assert st.run("MATCH (c:Commit) RETURN count(c) AS c")[0]["c"] == 4

        hist = q.history(PLACE)
        assert hist[0]["sha"] == shas["c2"]
        assert hist[0]["work_items"][0]["id"] == "wi:4321" and hist[0]["work_items"][0]["via"] == "message"
        f = q.history(FIND)
        assert f[0]["sha"] == shas["merge"] and f[0]["pull_requests"][0]["number"] == 77
        assert f[0]["work_items"][0] == {"id": "wi:5555", "number": 5555, "type": "Product Backlog Item",
                                         "title": "Clear errors", "state": None, "via": "pr-branch",
                                         "confidence": 0.6}
        assert [x["sha"] for x in q.history(PLACE, work_item_types=["Bug"])] == [shas["c2"]]
        assert q.history(FIND, work_item_types=["Bug"]) == []
        # a class aggregates its methods' history
        cls = q.history("java:order-service:com.acme.order.service.OrderServiceImpl")
        assert {shas["c2"], shas["merge"]} <= {x["sha"] for x in cls}
        assert q.history("repo:order-service")[0]["sha"] == shas["c4"]

        wi = q.work_item("AB#5555")
        assert wi["title"] == "Clear errors" and wi["pull_requests"][0]["number"] == 77
        assert wi["commits"][0]["id"] == commit_id("order-service", shas["merge"])
        assert FIND in {t["id"] for t in wi["touched_code"]}
        assert "error" in q.work_item("999999")
        # work items are found by full-text search
        assert q.search("Total is wrong", kinds=["WorkItem"])[0]["id"] == "wi:4321"
    finally:
        st.run("MATCH (n:Node {src: 'history'}) DETACH DELETE n")
        st.close()
