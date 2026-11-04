# TFS Knowledge Extractor

Builds a knowledge graph of the team's Azure DevOps Server repositories (Java/Spring Boot and React) in Neo4j,
and serves it to coding agents (opencode, Claude Code) through an MCP server.
Design and roadmap: [ARCHITECTURE.md](ARCHITECTURE.md).

**Status: phase 1 (code graph).** Phases 2–4 (commits/PRs/work items, Confluence, LLM enrichment) are not built yet.

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

## Known limits (phase 1)

- Java call resolution works from syntax only (no compiler classpath): receivers typed by locals, params, fields,
  static imports and `this`. Chained calls only resolve when the method name is unique (confidence 0.4).
  Lombok-generated methods and calls into external libraries produce no edges.
- Mapping paths built from constants in *other* classes (`ApiPaths.ORDERS + "/{id}"`) keep `{}` for the unknown
  part, so they match as suffixes (confidence 0.6).
- Only each repository's default branch is indexed.
