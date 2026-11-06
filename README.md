# TFS Knowledge Extractor

Builds a knowledge graph of the team's Azure DevOps Server repositories (Java/Spring Boot and React) in Neo4j,
and serves it to coding agents (opencode, Claude Code) through an MCP server.
Design and roadmap: [ARCHITECTURE.md](ARCHITECTURE.md).

**Status: phase 1 (code graph) and phase 2 (commits, pull requests, work items).** Phases 3–4 (Confluence +
semantic search, LLM enrichment) are not built yet.

## Layout

```
src/kg/
  sources/ado.py       Azure DevOps REST (list repos + default branch, PAT auth)
  sources/repos.py     bare git mirrors (prod) / plain folders (dev); file classification
  parsers/java.py      tree-sitter Java: types, members, annotations, call sites with receiver types
  parsers/typescript.py tree-sitter TS/TSX: imports/exports, components, hooks, JSX, HTTP calls, routes
  parsers/manifests.py pom.xml, package.json, tsconfig, application*.yml/properties (keys only)
  build.py             global resolution -> nodes and edges (calls, DI, endpoints, cross-repo HTTP, deps)
  pipeline.py          fetch -> parse (sqlite cache by blob hash) -> build
  store.py             Neo4j schema, batched MERGE upserts, run-stamp sweep of stale facts
  queries.py           read-side queries behind the MCP tools
  mcp_server.py        MCP server (streamable HTTP, /mcp)
  cli.py               kg schema | ingest | serve | stats
deploy/k8s/            Rancher manifests: Neo4j, nightly ingest CronJob, MCP Deployment
```

## Run with Docker only (no Python needed)

GitHub Actions (`.github/workflows/docker.yml`) runs the tests, then publishes
`ghcr.io/hayridurmaz/tfs-knowledge-extractor` (`latest` from `main`, `0.1.0` from tag `v0.1.0`, `sha-<commit>`).

```bash
docker login ghcr.io                      # only while the repo/package is private (PAT with read:packages)
cp .env.example .env                      # optional: ADO URL, project, PAT
docker compose up -d                      # Neo4j + MCP server -> http://localhost:8000/mcp
docker compose run --rm ingest            # fetch from ADO and load the graph
docker compose run --rm ingest ingest --local-path /repos   # or index checkouts placed in ./repos
docker compose run --rm ingest stats
```

### Checking the ADO connection

Before the first ingest, list the repos the job would fetch (nothing is cloned):

```bash
docker compose run --rm --entrypoint python ingest -c "from kg.config import Settings; from kg.sources.ado import AdoClient; r=AdoClient(Settings()).list_repos(); print(len(r),'repos'); [print(x.name, x.default_branch, x.remote_url) for x in r[:10]]"
```

| Error | Meaning |
|---|---|
| `401` | PAT is wrong or lacks the Code (Read) scope |
| `404` | `KG_ADO_URL` (collection URL, no project, no trailing slash) or `KG_ADO_PROJECT` is wrong |
| `CERTIFICATE_VERIFY_FAILED` | Internal CA: put it in `./certs/ca.crt` and set `KG_CA_BUNDLE=/certs/ca.crt` |
| `ConnectTimeout` | The TFS host is not reachable from the container: check the proxy (`HTTPS_PROXY` in `.env`), VPN/Docker networking, and the port/scheme in `KG_ADO_URL` |

### Troubleshooting `ConnectTimeout`

The container resolved the host but got no answer at the network level, so the PAT and URL path are not the problem yet.
Compare what the container sees with what your PC can reach (replace the host and port with the ones in `KG_ADO_URL`):

**1. Settings the container actually uses** (the PAT is not printed):
```bash
docker compose run --rm --entrypoint python ingest -c "from kg.config import Settings; s=Settings(); print('url=', repr(s.ado_url)); print('project=', repr(s.ado_project)); print('pat set=', bool(s.ado_pat)); import os; print('proxy=', os.environ.get('HTTPS_PROXY'))"
```
`url` must match the browser address exactly, including `http`/`https` and any port (TFS often runs on `:8080`).

**2. From your PC** (PowerShell):
```powershell
Test-NetConnection tfs.example.local -Port 443
Resolve-DnsName tfs.example.local
curl.exe -v -u ":YOUR_PAT" "https://tfs.example.local/tfs/DefaultCollection/_apis/projects?api-version=5.0"
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' | Select ProxyEnable,ProxyServer,AutoConfigURL
```

**3. From the container:**
```bash
docker compose run --rm --entrypoint python ingest -c "import socket; h='tfs.example.local'; p=443; print('ip:', socket.gethostbyname(h)); socket.create_connection((h,p),5); print('tcp ok')"
```

| PC (step 2) | Container (step 3) | Cause and fix |
|---|---|---|
| Fails | Fails | Wrong host, port or scheme, or TFS is only reachable through the browser's proxy |
| Works | Different IP than `Resolve-DnsName` | Split DNS: add `extra_hosts: ["tfs.example.local:10.x.x.x"]` to the `ingest` service |
| Works | Same IP, times out | Proxy: if `ProxyServer`/`AutoConfigURL` is set, put `HTTPS_PROXY=http://<proxy>:<port>` in `.env` (for a PAC file, open it to find the proxy). No proxy: the VPN is not routed into the Docker VM; check Docker Desktop → Settings → Resources → Network/Proxies or enable WSL mirrored networking, then restart Docker |
| Works | `tcp ok` | Network is fine: re-check `KG_ADO_URL` against the browser address |

If TFS is internal and the proxy blocks it, add the TFS host to `NO_PROXY` instead.

## Local quickstart

```bash
py -3.12 -m venv .venv && .venv/Scripts/activate        # Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest                                                    # unit tests (Neo4j tests skip without KG_NEO4J_URI)

docker compose up -d neo4j
export KG_NEO4J_PASSWORD=devpassword
kg ingest --local-path tests/fixtures/repos               # or a folder holding real repo checkouts
kg stats
kg serve                                                  # http://localhost:8000/mcp

KG_NEO4J_URI=bolt://localhost:7687 pytest                 # + integration tests (WIPES the database)
```

## Against Azure DevOps Server

```bash
export KG_ADO_URL=https://tfs.example.local/tfs/DefaultCollection
export KG_ADO_PROJECT=YourProject
export KG_ADO_PAT=...            # Code (Read) scope is enough for phase 1
export KG_CA_BUNDLE=/path/ca.crt # if TFS uses an internal CA (or KG_VERIFY_SSL=false, not recommended)
kg ingest                        # mirrors into ./data/mirrors, cache in ./data/parse_cache.sqlite
kg ingest --repos order-service  # fetch only one repo (graph is still built from all mirrors)
kg ingest --skip-fetch --dry-run # rebuild from existing mirrors and print counts only
```

All settings are `KG_*` environment variables, see [src/kg/config.py](src/kg/config.py).

## History: commits, pull requests, work items

`kg history` runs after `kg ingest` (it reads the same git mirrors) and answers "which PBIs and bugs touched this
method, and why":

```bash
docker compose run --rm ingest history                  # or: kg history
docker compose run --rm ingest history --repos order-service --skip-work-items
```

- **Commits**: `git log --first-parent` of the default branch. Each diff hunk is mapped to the methods/functions of
  the file at that commit (re-parsed through the parse cache), plus a File-level edge.
- **Pull requests**: completed PRs from the ADO API, linked to their merge commit.
- **Work items**: types in `KG_WORK_ITEM_TYPES` changed in the window, plus every item that commits/PRs refer to
  and their parents. Links come from ADO artifact links (confidence 1.0), `AB#123` / `#123` in messages (0.8), and
  numbers in PR branch names like `feature/123-x` (0.6).
- The first run covers `KG_HISTORY_MONTHS` (24); later runs only add new commits and changed items. The PAT needs
  **Code (Read)** and **Work Items (Read)**.

MCP tools: `history(id)` (commits + PRs + work items for a method/class/file/repo, newest first, optional
`work_item_types=["Bug"]`) and `work_item(id)` (fields, parent/children, PRs, commits, changed code).

## Run on a server (docker compose + cron)

On a Linux VM with Docker (about 16 GB RAM for ~100 repos):

```bash
git clone https://github.com/hayridurmaz/tfs-knowledge-extractor.git /opt/kg && cd /opt/kg
cp .env.example .env              # ADO URL/project/PAT, KG_IMAGE pinned to a version, Neo4j password
docker login ghcr.io              # while the package is private
docker compose up -d              # Neo4j + MCP server on :8000
./deploy/nightly.sh               # first full run (ingest + history); takes a while
crontab -e                        # 0 2 * * * /opt/kg/deploy/nightly.sh >> /opt/kg/nightly.log 2>&1
```

Agents then connect to `http://<vm>:8000/mcp`. Set `NEO4J_HEAP` / `NEO4J_PAGECACHE` in `.env` (e.g. 4G / 4G) and keep
port 8000 on the internal network: the graph contains all source code.

## Deploy to Rancher

1. Build and push the image through Nexus:
   `docker build --build-arg PIP_INDEX_URL=<nexus pypi proxy> --build-arg BASE_IMAGE=<nexus>/python:3.12-slim -t <registry>/kg:0.1.0 .`
2. Edit `deploy/k8s/config.yaml` (ADO URL and project) and `kustomization.yaml` (image registry).
3. Create the secret (see `deploy/k8s/secret.example.yaml`), then `kubectl apply -k deploy/k8s`.
4. First run on demand: `kubectl -n kg create job --from=cronjob/kg-ingest kg-ingest-first`.

The ingest pod resolves the whole graph in memory. Give it ~12 GiB for ~100 repos and adjust after the first run.
The graph contains all source code, so keep the MCP Ingress on the internal network.

## Connect agents

**Claude Code**
```bash
claude mcp add --transport http kg http://kg-mcp.example.local/mcp
```
**opencode** (`opencode.json`)
```json
{ "$schema": "https://opencode.ai/config.json",
  "mcp": { "kg": { "type": "remote", "url": "http://kg-mcp.example.local/mcp", "enabled": true } } }
```

Add this to each repo's `AGENTS.md` / `CLAUDE.md` so agents reach for the graph:

```markdown
## Code knowledge graph (MCP server `kg`)
The local checkout is one of ~100 services. Before changing a public method, endpoint, DTO or shared library,
use the `kg` tools: `find_symbol`/`search` to locate it, `impact` to see callers, other services and
frontend screens that depend on it, `endpoints` to find who consumes an HTTP API. Edges with confidence < 0.6 are
leads; verify them in code.
```

## Known limits

History (phase 2):
- Only first-parent commits of the default branch: a merged PR appears as its merge commit (the diff against the
  first parent), not as the individual branch commits.
- A commit links to a method by today's id; if the method was later renamed or its parameters changed, only the
  File-level edge remains (and if the file is gone, the change is not linked to code).
- PR review comments and work item discussion comments are not loaded yet.

Code graph (phase 1):

- Java call resolution works from syntax only (no compiler classpath): receivers typed by locals, params, fields,
  static imports and `this`. Chained calls only resolve when the method name is unique (confidence 0.4).
  Lombok-generated methods and calls into external libraries produce no edges.
- Mapping paths built from constants in *other* classes (`ApiPaths.ORDERS + "/{id}"`) keep `{}` for the unknown
  part, so they match as suffixes (confidence 0.6).
- Only each repository's default branch is indexed.
