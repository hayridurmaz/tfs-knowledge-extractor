# TFS Knowledge Extractor — Architecture

A nightly pipeline that turns the team's Azure DevOps Server Git repos, work items, and Confluence
space into a **knowledge graph**. Coding agents (opencode, Claude Code) query it through an **MCP server**.

## 1. Constraints (decided 2026-11-04)

| Topic | Decision |
|---|---|
| Sources | Azure DevOps Server: 1 collection, 1 project, ~100 Git repos, PAT auth. Confluence (on-prem): 1 space, PAT auth |
| Code | Mostly Java / Spring Boot and React (TS/JS) |
| Branch | The default branch of each repo, as ADO reports it |
| LLM | DeepSeek flash via the company's OpenAI-compatible endpoint (0.5M context). Used **only for enrichment** |
| Embeddings | **Deferred** until we know whether the company offers an embedding endpoint. Neo4j full-text search covers search until then |
| Runtime | Rancher Kubernetes, packages through the Nexus proxy, open-source licenses allowed |
| Freshness | Nightly batch |
| Access control | None: one team, and all content may go to the internal LLM |
| Consumers | opencode and Claude Code over MCP (streamable HTTP) |
| Implementation | Python 3.12 |

## 2. Principle: parse the code, use the LLM only to enrich it

The graph's **structure** comes from parsers and APIs, never from an LLM:
tree-sitter ASTs, Spring annotations, Maven/npm manifests, ADO links, `AB#123` references, and git diffs.
That makes it repeatable, fast, and free. The LLM only adds things that need understanding:
summaries, business rules, and linking documentation to code (phase 4).

## 3. Components

```
                 Rancher cluster (namespace: kg)
┌──────────────────────────────────────────────────────────────┐
│  CronJob "kg-ingest" (nightly)                               │
│   1 FETCH    ADO REST: list repos + default branch           │
│              git fetch → bare mirrors on a PVC               │
│   2 PARSE    every source blob on the default branch         │
│              (sqlite parse cache keyed by blob SHA, so only  │
│              changed files are actually parsed)              │
│   3 RESOLVE  global, in memory: types, calls, DI, overrides, │
│              HTTP clients → endpoints, maven deps            │
│   4 LOAD     idempotent MERGE upserts stamped with run id,   │
│              then sweep anything not seen in this run        │
│   (phase 2)  commits / PRs / work items → method links       │
│   (phase 3)  Confluence pages → sections → MENTIONS          │
│   (phase 4)  LLM summaries / rules for changed content only  │
└───────────────┬──────────────────────────────────────────────┘
                ▼
        ┌──────────────────┐  graph + full-text indexes
        │ Neo4j 5 Community│  (+ vector index when embeddings arrive)
        └───────┬──────────┘
                ▼
        ┌──────────────────┐  streamable HTTP  /mcp
        │  kg-mcp server   │◄─────────── opencode / Claude Code
        └──────────────────┘
```

### Why the graph is rebuilt in full every night

The cache means only changed blobs are parsed, so each run still gets a complete in-memory model of all
repos. That model is resolved globally and written as the *desired state*. Nodes and edges get a `run` stamp,
and anything without the current stamp is removed. This keeps cross-repo edges correct (e.g. a frontend
call in an unchanged repo pointing at an endpoint that moved in a changed repo), and it needs no
incremental-delete logic. Expected size: ~50k types and ~500k methods, which is a few minutes of
UNWIND batch writes.

## 4. Graph schema

Every node has the label `:Node` plus a kind label, a stable readable `id`, `repo`, `path`, and `start_line`.

| Kind label | Example id | Notes |
|---|---|---|
| `Repository` | `repo:order-service` | default_branch, head_sha, service_name |
| `Module` | `module:order-service:core` | one per pom.xml / package.json |
| `Package` | `pkg:order-service:com.acme.order` | Java package |
| `File` | `file:order-service:src/main/java/...` | |
| `Class` (+ `Interface`/`Enum`/`Record`/`Annotation`) | `java:order-service:com.acme.OrderService` | stereotype, annotations, doc |
| `Method` | `java:order-service:com.acme.OrderService#place(OrderRequest)` | signature, code, lines |
| `Field` | `java:order-service:com.acme.OrderService#repo` | |
| `Endpoint` | `ep:order-service:GET:/orders/{id}` | Spring MVC mapping |
| `ApiCall` | `api:web-app:src/api.ts:12` | axios/fetch/RestTemplate/WebClient call site |
| `Table` | `table:orders` | from `@Table`/`@Entity` |
| `ConfigProperty` | `cfg:order-service:app.payment.url` | keys only, never values |
| `Topic` | `topic:order-created` | Kafka |
| `Function` (+ `Component`/`Hook`) | `ts:web-app:src/pages/Orders.tsx#OrdersPage` | React |
| `Route` | `route:web-app:/orders/:id` | react-router |
| `ExternalType`, `MavenArtifact`, `NpmPackage` | `mvn:com.acme:common-lib` | |

Edges (all carry `repo`, `run`, `src='code'`; inferred edges also carry `confidence`):

```
Repository-CONTAINS->Module|Package|File     File-CONTAINS->Class|Function
Class-CONTAINS->Method|Field                 Package-CONTAINS->Class
Class-EXTENDS|IMPLEMENTS->Class|ExternalType Method-OVERRIDES->Method
Method-CALLS{confidence,count}->Method       Class-INJECTS{via}->Class
Method-EXPOSES->Endpoint                     Method|Function-CALLS_API->ApiCall
ApiCall|Method(Feign)-CONSUMES{confidence}->Endpoint
Class-MAPS_TO->Table                         Class-REPOSITORY_OF->Class(entity)
Class|Method|Field-READS_CONFIG->ConfigProperty  File-DEFINES->ConfigProperty
Method-PUBLISHES|SUBSCRIBES->Topic
Function-RENDERS->Component                  Function-USES_HOOK->Hook
Function-CALLS->Function                     Route-ROUTES_TO->Component
File-IMPORTS->File|NpmPackage
Module-PUBLISHES_ARTIFACT->MavenArtifact     Module-DEPENDS_ON{scope}->MavenArtifact|NpmPackage
Repository-DEPENDS_ON_REPO->Repository
```

Later phases add: `Commit-MODIFIED->Method|File`, `PullRequest-CONTAINS->Commit`,
`PullRequest|Commit-RESOLVES->WorkItem`, `WorkItem-CHILD_OF->WorkItem`, `Person-AUTHORED->Commit`,
`ConfluencePage-HAS_SECTION->Section-MENTIONS->*`, `Section-DESCRIBES{confidence}->*`.

### How resolution works, and its precision

Java call resolution is heuristic, because tree-sitter only sees syntax and there's no compiler classpath.
The receiver type comes from locals, parameters, fields, static imports and `this`/`super`. Simple
type names are resolved through nested types, then single imports, then the same package, then
wildcard imports. Lookups walk the internal supertypes. When the receiver type is unknown (chained calls),
the resolver falls back to a method name that is unique across the whole graph, with
`confidence=0.4`. Interface dispatch goes through `OVERRIDES`, so `callers(impl)` also returns
callers of the interface method. If precision isn't good enough, the Java parser can be swapped for
JavaParser's symbol solver without changing the schema.

To match HTTP calls to endpoints, paths are normalized (host and query stripped, `{x}`, `:x` and `${x}` turned into `{}`).
Matches are either exact (0.9) or suffix matches that absorb gateway or context prefixes (0.6). A Feign
`name` that equals a repo's `spring.application.name` restricts the candidates and raises confidence.

## 5. MCP tools (phase 1)

| Tool | Purpose |
|---|---|
| `search(query, kinds?, repo?)` | Full-text search over names, FQNs, paths, signatures, javadoc and code |
| `find_symbol(name, kind?, repo?)` | Exact or suffix match on name/FQN |
| `get_node(id)` | Properties, source code, class outline, relationship counts |
| `neighbors(id, rel_types?, direction?)` | Generic one-hop navigation |
| `callers(id, depth)` / `callees(id, depth)` | Call graph (includes interface dispatch) |
| `impact(id)` | Upstream callers → exposed endpoints → consumers across repos → routes |
| `endpoints(path?, repo?, http_method?)` | Endpoints with their handler and consumers |
| `list_repos()` / `repo_overview(repo)` | Service map, modules, artifacts, repo dependencies |
| `graph_schema()` | Schema description, so agents can write Cypher |
| `cypher(query, params?)` | Read-only escape hatch (READ access mode, timeout, row cap) |

Later phases add `history(id)`, `work_item(id)`, and `docs_for(id)`.

## 6. Phases

1. **Code graph** (this scaffold): all repos, Java and React structure, calls, Spring endpoints,
   cross-repo `CONSUMES` edges, Maven/npm dependencies, the MCP server, and full-text search.
2. **History**: default-branch commits (diff hunks mapped to method line ranges), PRs with review
   threads, and PBI/Bug/Feature/Task items changed in the last 24 months plus any older item linked
   from history. Links come from ADO artifact links, `AB#123`, `#123`, and `feature/123` branch names.
3. **Confluence**: pages in storage format converted to markdown and split into sections by heading,
   linked to code by exact-name matching against graph names. Embeddings and hybrid search are added
   once an embedding model is available (self-hosted bge-m3 is the fallback).
4. **LLM enrichment**: bottom-up summaries (class → package → module → repo), business rules,
   fuzzy doc↔code links. Gated by `hash`/`summary_hash`, so only changed content costs tokens.
5. **Hardening**: a test set of 20–30 real team questions, to measure retrieval quality with every change.

## 7. Deferred to later phases

Pipelines and builds, Confluence attachments (PDF/Word/draw.io), test-case work items, feature branches,
and per-user permissions.
