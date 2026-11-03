# Handoff / Plan: TFS Knowledge Extractor

Last updated: 2026-11-04. Read this first when resuming on another machine.
Design: [ARCHITECTURE.md](ARCHITECTURE.md). Setup and usage: [README.md](README.md).

---

## 1. Environment facts (from the user, 2026-11-03)

| Topic | Fact |
|---|---|
| Source control / work items | Azure DevOps Server (version unknown), **PAT auth**, 1 collection, 1 project, ~100 **Git** repos, PBIs and Bugs |
| Docs | Confluence on-prem (version unknown), **PAT auth**, 1 space |
| Code | Mostly Java (Spring Boot) and React |
| LLM | DeepSeek "flash" via the company's OpenAI-compatible endpoint, 0.5M context. **No embedding model confirmed** |
| Infra | Rancher Kubernetes; Nexus proxy for pip/npm/Docker; any open-source license OK |
| Content language | Mixed (Turkish + English), so multilingual embeddings are needed later |
| Consumers | opencode and Claude Code (MCP) |
| Scope | One team. No per-user permissions. Sending all content to the internal LLM is OK. Nightly batch is enough. Service account available |
| Work items | Many; time-filtered ingestion is acceptable |

## 2. Decisions taken

- **Python 3.12**, tree-sitter parsers, **Neo4j 5.26 Community** (graph + full-text), **MCP server** (`mcp` SDK **2.x**:
  `from mcp.server.mcpserver import MCPServer`; streamable HTTP, stateless).
- Index the **default branch** of each repo only.
- The graph's structure is deterministic (parsers, APIs, regexes). **The LLM is used only for enrichment** (phase 4).
- **Embeddings are deferred.** The user is checking whether the company offers an embedding endpoint. Fallback: self-host
  `bge-m3` with HuggingFace TEI on Rancher. Neo4j full-text search covers search until then.
- Each nightly run rebuilds the desired state in full (cheap thanks to the parse cache keyed by blob SHA), then sweeps
  stale nodes and edges using the `run` stamp.

## 3. Status

### Phase 1: code graph (DONE)

Built and verified on the original machine:
- `pytest`: **25 passed**, including the Neo4j integration tests (sweep, idempotent rerun, impact → frontend route,
  read-only Cypher).
- `kg ingest --local-path tests/fixtures/repos` → Neo4j, then `kg serve`, called by a real MCP client over HTTP
  (all 12 tools listed; `impact` returned endpoint → frontend consumer → route; Cypher writes rejected).
- Synthetic scale run: 60 repos / 12k Java files. Parse: 175 s cold, 6.5 s from cache. Build: 205k nodes,
  739k edges, ~580 MB peak memory. No quadratic hotspot in the profile.

**Not yet verified (do these first):**
1. **Against the real ADO server.** `AdoClient.list_repos` and `GitMirror.fetch` (PAT via `http.extraHeader`,
   API version `5.0`) have never touched the real server. Check: ADO Server version, TLS/CA (`KG_CA_BUNDLE`), and
   whether `remoteUrl` from the API is reachable from the cluster.
2. **Building the Docker image** (`Dockerfile`) through Nexus.
3. **The k8s manifests** (`deploy/k8s/`) on Rancher: storage class, registry name, Ingress host.
4. **Precision on real code.** Spot-check `CALLS` and `CONSUMES` edges on 2–3 real services. Look at
   `parse_errors` on File nodes and at counts from `kg ingest --skip-fetch --dry-run`.

### Phase 2: history (NEXT, not started)

Goal: `history(id)` and `work_item(id)` MCP tools that answer "which PBIs and bugs touched this method, and why".

Planned design:
- **Commits** (from the local mirrors, no API needed):
  `git log --first-parent <default> --since=<24 months> --format=... -p --unified=0` per repo.
  Map diff hunks (new-file line ranges) to method/function `start_line..end_line` at that commit.
  - Cheap first version: map onto the **current** HEAD line ranges only for files unchanged since the commit, and use
    File-level `MODIFIED` for everything else.
  - Later: re-parse the file at the commit's blob (the cache makes this cheap) for exact method mapping.
  - Nodes: `Commit{sha, message, author, date}`, `Person{name, email}`.
  - Edges: `Commit-MODIFIED{added, deleted}->Method|Function|File`, `Person-AUTHORED->Commit`.
- **Pull requests** (ADO REST):
  - Listing: `GET {project}/_apis/git/repositories/{repoId}/pullrequests?searchCriteria.status=completed&$top=..&$skip=..`
  - Commits: `GET .../pullrequests/{id}/commits`
  - Linked work items: `GET .../pullrequests/{id}/workitems`
  - Threads (review comments; keep non-system ones): `GET .../pullrequests/{id}/threads`
  - Nodes and edges: `PullRequest{id, title, description, status, created, closed, target}`, `PR-CONTAINS->Commit`,
    `PR-RESOLVES->WorkItem`, `ReviewComment` (or fold the comments into a PR property).
- **Work items** (ADO REST):
  - Query with WIQL: `POST {project}/_apis/wit/wiql` with
    `[System.ChangedDate] >= @Today - 730 AND [System.WorkItemType] IN ('Product Backlog Item','Bug','Feature','Epic','Task')`.
  - Then batch-fetch: `GET _apis/wit/workitems?ids=..(≤200)&$expand=relations`.
  - Comments: `GET {project}/_apis/wit/workItems/{id}/comments`. This API is preview on older servers; fall back to
    `System.History` revisions.
  - Fields: title, description, acceptance criteria (`Microsoft.VSTS.Common.AcceptanceCriteria`), repro steps
    (`Microsoft.VSTS.TCM.ReproSteps`), state, area/iteration path, tags, priority/severity. Convert HTML to text.
  - Edges: `WorkItem-CHILD_OF->WorkItem` (from `System.LinkTypes.Hierarchy-Reverse`), and
    `Commit|PR-RESOLVES->WorkItem` from ADO artifact links, `AB#123` / `#123` in messages, and branch names like `feature/123`.
  - Also pull in older work items that are linked from history inside the window.
- **Incrementality**: watermarks in `(:IngestState {id:'history'})`: last commit SHA per repo, max PR id/closed date,
  max work item `ChangedDate`. History facts use `src='history'`, so the code sweep never deletes them. If a
  node's target method disappears, keep a File-level edge.
- **New MCP tools**: `history(id, limit)` returns commits, PRs and work items touching a node, newest first.
  `work_item(id)` returns fields, children/parent, linked PRs/commits and touched methods. Bugs touching a node can be
  a filter on `history`.
- **PAT scopes** needed: Code (Read) and Work Items (Read).
- **Planned code layout**: `src/kg/sources/ado.py` (add PR and WIQL methods), `src/kg/history.py` (git log parsing
  + hunk→method mapping), `src/kg/store.py` (history writer), `src/kg/queries.py` + `mcp_server.py` (tools),
  `tests/test_history.py` (a fixture repo with a few commits made in a temp dir, like `tests/test_sources.py`).

### Phase 3: Confluence + semantic search (blocked on the embedding decision)

- Confluence REST: `GET /rest/api/content?spaceKey=..&expand=body.storage,version,ancestors` (paged), incremental
  via CQL `space=X and lastmodified > "<watermark>"`.
- Convert the storage format to markdown, split into sections by heading. Nodes and edges: `ConfluencePage`,
  `Section`, `Page-CHILD_OF->Page`.
- Linking: exact matches against graph names (class names, endpoint paths, repo names, `PBI-123`/work item IDs)
  → `MENTIONS`.
- Embeddings for Section, WorkItem, and Class/Method summaries → Neo4j vector index → hybrid `search`
  (full-text + vector). Add a `docs_for(id)` tool.

### Phase 4: LLM enrichment (DeepSeek flash)

- Bottom-up summaries (class → package → module → repo), stored as `summary` plus `summary_hash` and recomputed only
  when the node's `hash` changes. The upsert uses `SET n += props`, so summaries survive nightly runs.
- Business rules / domain concepts extracted from PBIs, Confluence and code → `Concept`/`BusinessRule` nodes,
  plus fuzzy `DESCRIBES{confidence}` doc↔code links.

### Phase 5: evaluation

- Collect 20–30 real questions from the team and record the expected answers. Score retrieval after every change.

## 4. Open questions for the user

1. Does the company provide an embedding endpoint (which model)? This blocks phase 3.
2. Which ADO Server version? (`_apis` 5.0 is assumed.) Is a corporate CA needed?
3. Which work item types and states matter, and is 24 months the right window?
4. Is a GPU node available on Rancher, if we self-host embeddings?

## 5. Resuming on a new machine

```bash
# copy/clone the project (it is NOT a git repo yet: `git init` and push to ADO first)
py -3.12 -m venv .venv && .venv/Scripts/activate          # Linux/mac: python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                                     # 17 pass + integration skipped without Neo4j
docker compose up -d neo4j
KG_NEO4J_URI=bolt://localhost:7687 KG_NEO4J_PASSWORD=devpassword pytest   # all 25 should pass (wipes the DB)
```

Notes and gotchas found while building:
- `mcp` is pinned `>=2.3,<3`. The v2 API differs from v1: `MCPServer` replaces `FastMCP`, and client results use
  snake_case (`server_info`, `structured_content`, `is_error`).
- In the neo4j driver, a timeout inside managed transactions needs `@unit_of_work(timeout=..)`. `Query(timeout=..)` only
  works with `session.run`.
- `GET /mcp` opens a never-ending SSE stream, so don't use `curl` on it as a readiness probe (k8s uses a tcpSocket probe).
- `.js` files are parsed with the TSX grammar (React `.js` files contain JSX).
- Bump `PARSER_VERSION` in `src/kg/parsers/__init__.py` whenever parser output changes, or the cache returns stale results.

Suggested first prompt in the new session:
> "Read PLAN.md and ARCHITECTURE.md, verify the test suite passes, then start phase 2 (history)."
