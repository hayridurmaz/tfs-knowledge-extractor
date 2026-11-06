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
| LLM | DeepSeek "flash" via the company's OpenAI-compatible endpoint, 0.5M context |
| Embeddings (2026-11-04) | Company hosts **`intfloat/multilingual-e5-large`** and **`Qwen3-Embedding-8B`**. Endpoint URL / API shape not yet confirmed |
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
- **Embeddings: use the company-hosted `Qwen3-Embedding-8B`** (proposed, 2026-11-04). It has a 32k-token context (fits whole
  methods and Confluence sections), is strong on multilingual TR+EN and code, and supports Matryoshka truncation, so store
  **1024 dims** (request `dimensions=1024`, or truncate client-side and L2-renormalize) to keep the Neo4j vector index small.
  Queries get an instruction prefix (`Instruct: <task>\nQuery: <q>`); documents get none.
  Fallback: `multilingual-e5-large` (1024 dims, **512-token limit**, needs `query: ` / `passage: ` prefixes, weaker on code).
  Self-hosting `bge-m3` is no longer needed. Neo4j full-text search remains the keyword half of hybrid search.
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

**Verified on the real environment (2026-11-06):**
- Docker image built and published by GitHub Actions to `ghcr.io/hayridurmaz/tfs-knowledge-extractor` (tag `v0.1.0`),
  run with `docker compose` (no local Python).
- Full ingest against the real ADO server succeeded: **467,020 nodes, 1,068,467 edges**, build 44 s (the Neo4j edge
  write is the slow part and logs no progress yet). Fixed on the way: proxy pass-through in compose, and over-long
  range-indexed values ("Property value is too large to index") capped in `store.py`.

**Still open:**
1. **Precision on real code.** Spot-check `CALLS` and `CONSUMES` edges on 2–3 real services. Look at
   `parse_errors` on File nodes and at the `capped over-long indexed value` warnings from the ingest log.
2. **Production host:** decided: docker compose on a Linux VM with a nightly cron (`deploy/nightly.sh`, README
   "Run on a server"). The k8s manifests stay for a later move to Rancher.

### Phase 2: history (BUILT 2026-11-06, not yet run against the real server)

Implemented as designed below, with these choices: hunks are mapped by **re-parsing the file at each commit's blob**
(exact method mapping, through the parse cache) instead of the cheap HEAD-only version; PR→commit uses the PR's
`lastMergeCommit` (no per-PR commits/work-items calls: work item `ArtifactLink`s already give PR/commit→WI links);
PR review threads and work item comments are **not** loaded yet. Code: `src/kg/history.py`, `kg history` CLI,
`Store.write_history`, `Queries.history/work_item`, MCP tools `history` and `work_item`, `tests/test_history.py`.
Production runs on a server with docker compose + cron (`deploy/nightly.sh`), decided 2026-11-06.

Original design:

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

### Phase 3: Confluence + semantic search (unblocked: Qwen3-Embedding-8B, see §2)

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

1. ~~Embedding endpoint?~~ e5-large and Qwen3-Embedding-8B, OpenAI-compatible `/v1/embeddings` (2026-11-06).
   Still unknown: whether it honors `dimensions`, and batch/rate limits.
2. ~~ADO Server version / CA?~~ API 5.0 works against the real server (2026-11-06).
3. ~~Work item types and window?~~ Default type list and the 24-month window are OK; PAT has Work Items (Read).
4. ~~Production host?~~ Docker compose on a server (2026-11-06).

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
