"""MCP server exposing the knowledge graph to coding agents (streamable HTTP at /mcp)."""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from kg.config import Settings
from kg.queries import Queries
from kg.store import driver

INSTRUCTIONS = """\
Knowledge graph of the team's ~100 repositories (Java/Spring Boot backends, React frontends), default branches,
rebuilt nightly. Use it for anything beyond the repo you have checked out: who calls this, which service exposes
an endpoint, which frontend screen hits it, what a change would impact.

Workflow:
1. Locate: `search` (keywords, names, paths; set search_code=true to find string literals / error messages)
   or `find_symbol` (exact class/method/function name or FQN). Results carry an `id`.
2. Inspect: `get_node(id)` returns code, signature, members and relationship counts.
3. Navigate: `callers` / `callees` (interface dispatch included), `impact` (upstream callers -> exposed endpoints ->
   consumers in other repos -> UI routes), `endpoints` (HTTP API map), `neighbors` (any relationship).
4. Service level: `list_repos`, `repo_overview`.
   Why is the code like this: `history(id)` lists the commits, pull requests and work items (PBIs, bugs) that
   touched a method/class/file, newest first; `work_item(id)` shows a PBI/bug with its parent, children, PRs,
   commits and the code it changed. `search` also finds commits, PRs and work items by text.
5. Anything else: `graph_schema` then read-only `cypher`.

Edges inferred heuristically carry `confidence` (0-1); treat < 0.6 as a lead to verify in code, not a fact.
"""

SCHEMA = """\
All nodes: label :Node + a kind label, unique `id`, `name`, `repo`, `path`, `start_line`, `tokens`.
Kinds: Repository, Module, Package, File, Class (+Interface|Enum|Record|Annotation|Controller|FeignClient|
JpaEntity|SpringRepository|Component), Method, Field, Endpoint{http_method,path,path_norm,handler},
ApiCall{http_method,url,url_norm,client}, Table, ConfigProperty{key,defined}, Topic,
Function (+Component|Hook), Route{path}, ExternalType, MavenArtifact, NpmPackage. Test code also has :Test.
Method: signature, params, return_type, annotations, code, class_fqn. Class: fqn, kind, stereotype, annotations, doc.

Relationships (direction as written):
(Repository)-[:CONTAINS]->(Module|Package|File); (Module)-[:CONTAINS]->(File)
(File)-[:CONTAINS]->(Class|Function); (Package)-[:CONTAINS]->(Class); (Class)-[:CONTAINS]->(Method|Field|Class)
(Class)-[:EXTENDS|IMPLEMENTS]->(Class|ExternalType); (Method)-[:OVERRIDES]->(Method)
(Method)-[:CALLS {confidence,count,lines}]->(Method); (Method)-[:INSTANTIATES]->(Class)
(Class)-[:INJECTS {via}]->(Class)
(Method)-[:EXPOSES]->(Endpoint); (Method|Function|File)-[:CALLS_API]->(ApiCall)
(ApiCall|Method)-[:CONSUMES {confidence,kind}]->(Endpoint)       # kind: frontend|rest|feign
(Class)-[:MAPS_TO]->(Table); (Class)-[:REPOSITORY_OF]->(Class)
(Class|Method|Field)-[:READS_CONFIG]->(ConfigProperty); (File)-[:DEFINES]->(ConfigProperty)
(Method)-[:PUBLISHES|SUBSCRIBES]->(Topic)
(Function|File)-[:CALLS|USES_HOOK|RENDERS]->(Function); (Function|File)-[:DEFINES_ROUTE]->(Route)
(Route)-[:ROUTES_TO]->(Function); (File)-[:IMPORTS]->(File|NpmPackage)
(Module)-[:PUBLISHES_ARTIFACT]->(MavenArtifact); (Module)-[:DEPENDS_ON {scope,version}]->(MavenArtifact|NpmPackage)
(Repository)-[:DEPENDS_ON_REPO {via}]->(Repository)

History (src='history'; default branch, first-parent, last ~24 months):
Commit{sha,short,date(UTC ISO),author,email,message,is_merge}, Person{email}, PullRequest{number,title,status,
source_branch,target_branch,created,closed,merge_commit}, WorkItem{number,type,title,state,area,iteration,tags,
priority,severity,assigned_to,created,changed,description,acceptance_criteria,repro_steps} (+Bug|PBI|UserStory|
Feature|Epic|Task). Ids: commit:<repo>:<sha>, pr:<n>, wi:<n>, person:<email>.
(Person)-[:AUTHORED]->(Commit); (Commit)-[:MODIFIED {added,deleted,status}]->(File)
(Commit)-[:MODIFIED {hunks}]->(Method|Function|Class); (PullRequest)-[:MERGED_AS]->(Commit)
(Commit|PullRequest)-[:RESOLVES {via,confidence}]->(WorkItem)   # via: link 1.0 | message 0.8 | branch 0.6
(WorkItem)-[:CHILD_OF]->(WorkItem)

Full-text indexes: 'node_text' (names, fqn, path, signature, doc, tokens, url, key) and 'node_code' (code).
Example: MATCH (c:Controller {repo:'order-service'})-[:CONTAINS]->(m)-[:EXPOSES]->(e) RETURN m.id, e.path
"""


def create_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings()
    q = Queries(driver(settings), settings.neo4j_database)
    mcp = MCPServer(name="kg", title="Team code knowledge graph", instructions=INSTRUCTIONS, version="0.2.1")

    @mcp.tool()
    def search(query: str, kinds: list[str] | None = None, repo: str | None = None, limit: int = 20,
               search_code: bool = False) -> list[dict]:
        """Full-text search over names, FQNs, paths, signatures and javadoc (search_code=true: phrase search in
        source code). `kinds` filters by label, e.g. ["Class","Method"], ["Endpoint"], ["Component"]."""
        return q.search(query, kinds, repo, limit, search_code)

    @mcp.tool()
    def find_symbol(name: str, kind: str | None = None, repo: str | None = None) -> list[dict]:
        """Find nodes by exact id, name or fully-qualified name (falls back to FQN suffix / case-insensitive)."""
        return q.find_symbol(name, kind, repo)

    @mcp.tool()
    def get_node(id: str, include_code: bool = True) -> dict:
        """All properties of a node (incl. source code), its parent, members (for classes/files/modules) and
        relationship counts by type and direction."""
        return q.get_node(id, include_code) or {"error": f"node not found: {id}"}

    @mcp.tool()
    def neighbors(id: str, rel_types: list[str] | None = None,
                  direction: Literal["in", "out", "both"] = "both", limit: int = 50) -> list[dict]:
        """One-hop navigation along any relationship types (see graph_schema)."""
        return q.neighbors(id, rel_types, direction, limit)

    @mcp.tool()
    def callers(id: str, depth: int = 2, limit: int = 100) -> list[dict]:
        """Who calls this method/function (transitively up to `depth`, max 5). Includes callers that go
        through an interface method this one implements (via_interface=true), hooks and JSX rendering."""
        return q.callers(id, depth, limit)

    @mcp.tool()
    def callees(id: str, depth: int = 2, limit: int = 100) -> list[dict]:
        """What this method/function calls (transitively), incl. implementations of called interface
        methods, HTTP calls (ApiCall) and instantiated classes."""
        return q.callees(id, depth, limit)

    @mcp.tool()
    def impact(id: str, depth: int = 3) -> dict:
        """Blast radius of changing a method/class/file: upstream callers, endpoints they expose, consumers of
        those endpoints in other repos (frontend + services), their callers, UI routes, and Kafka topics."""
        return q.impact(id, depth)

    @mcp.tool()
    def endpoints(path: str | None = None, repo: str | None = None, http_method: str | None = None,
                  limit: int = 50) -> list[dict]:
        """HTTP endpoints with handler method and consumers across repos. `path` may contain params in any
        style ('/orders/{id}', '/orders/:id', '/orders/123' does not match literally; use '/orders/')."""
        return q.endpoints(path, repo, http_method, limit)

    @mcp.tool()
    def list_repos() -> list[dict]:
        """All repositories with service names, size counts and repo-level dependencies."""
        return q.list_repos()

    @mcp.tool()
    def repo_overview(repo: str) -> dict:
        """Modules/artifacts, dependencies, top packages, and HTTP traffic to/from other repos for one repo."""
        return q.repo_overview(repo)

    @mcp.tool()
    def history(id: str, limit: int = 20, work_item_types: list[str] | None = None) -> list[dict] | dict:
        """Commits that changed a method/function/class/file (or a whole repo), newest first, each with its pull
        request and the work items (PBIs, bugs) it resolves. Use it to learn WHY code looks the way it does.
        `work_item_types` keeps only commits linked to those types, e.g. ["Bug"]."""
        return q.history(id, limit, work_item_types)

    @mcp.tool()
    def work_item(id: str) -> dict:
        """A work item (PBI, bug, feature...) by number or id ('12345', 'wi:12345', 'AB#12345'): fields incl.
        description / acceptance criteria / repro steps, parent and children, pull requests, commits, and the
        methods and files those commits changed."""
        return q.work_item(id)

    @mcp.tool()
    def graph_schema() -> str:
        """Node labels, properties and relationship types, for writing `cypher` queries."""
        return SCHEMA

    @mcp.tool()
    def cypher(query: str, params: dict[str, Any] | None = None, limit: int = 200) -> list[dict]:
        """Run a READ-ONLY Cypher query (15 s timeout, max `limit` rows). Call graph_schema first."""
        return q.cypher(query, params, limit)

    return mcp


def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    create_server(settings).run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port,
                                stateless_http=True)
