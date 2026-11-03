"""Ingestion pipeline: fetch -> parse (cached) -> build graph -> load."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
import zlib
from pathlib import Path

from kg.build import ParsedFile, ParsedRepo, build_graph
from kg.config import Settings
from kg.graph import Graph
from kg.parsers import PARSER_VERSION
from kg.parsers.java import parse_java
from kg.parsers.manifests import parse_package_json, parse_pom, parse_spring_config, parse_tsconfig
from kg.parsers.typescript import parse_ts
from kg.sources.repos import GitMirror, LocalRepo, RepoSnapshot, SourceFile

log = logging.getLogger(__name__)


def parse_file(f: SourceFile, data: bytes) -> dict:
    k = f.kind
    if k == "java":
        return parse_java(data)
    if k in ("ts", "tsx"):
        return parse_ts(data, tsx=(k == "tsx"))
    if k == "pom":
        return parse_pom(data)
    if k == "package_json":
        return parse_package_json(data)
    if k == "tsconfig":
        return parse_tsconfig(data)
    if k == "spring_config":
        return parse_spring_config(data, is_properties=f.path.endswith(".properties"))
    raise ValueError(k)


class ParseCache:
    """Parse results keyed by (parser version, kind, blob hash) so unchanged files are never re-parsed."""

    def __init__(self, path: Path | None) -> None:
        self.db = sqlite3.connect(str(path) if path else ":memory:")
        self.db.execute("CREATE TABLE IF NOT EXISTS parse (k TEXT PRIMARY KEY, v BLOB)")

    @staticmethod
    def key(f: SourceFile) -> str:
        return f"{PARSER_VERSION}:{f.kind}:{f.blob_id}"

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = f"SELECT k, v FROM parse WHERE k IN ({','.join('?' * len(chunk))})"
            for k, v in self.db.execute(q, chunk):
                out[k] = json.loads(zlib.decompress(v))
        return out

    def put(self, key: str, value: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO parse VALUES (?, ?)",
                        (key, zlib.compress(json.dumps(value, separators=(",", ":")).encode())))

    def commit(self) -> None:
        self.db.commit()


def parse_repo(snap: RepoSnapshot, cache: ParseCache) -> tuple[ParsedRepo, dict]:
    files = snap.files()
    keys = {f: ParseCache.key(f) for f in files}
    cached = cache.get_many(list(keys.values()))
    todo = [f for f in files if keys[f] not in cached]
    stats = {"files": len(files), "parsed": 0, "cached": len(files) - len(todo), "failed": 0}
    results: dict[SourceFile, dict] = {f: cached[keys[f]] for f in files if keys[f] in cached}
    for f, data in snap.read_many(todo):
        try:
            results[f] = parse_file(f, data)
            cache.put(keys[f], results[f])
            stats["parsed"] += 1
        except Exception as e:  # one bad file must not fail the whole run
            stats["failed"] += 1
            log.warning("%s: failed to parse %s: %s", snap.name, f.path, e)
    cache.commit()
    repo = ParsedRepo(snap.name, snap.default_branch, snap.head_sha,
                      [ParsedFile(f.path, f.kind, results[f]) for f in files if f in results])
    return repo, stats


def fetch_mirrors(settings: Settings, only: set[str] | None = None) -> list[str]:
    """Fetch/refresh bare mirrors from Azure DevOps. Returns repo names that failed."""
    from kg.sources.ado import AdoClient

    failed = []
    settings.mirrors_dir.mkdir(parents=True, exist_ok=True)
    for repo in AdoClient(settings).list_repos():
        if only and repo.name not in only:
            continue
        t = time.time()
        try:
            GitMirror.fetch(settings, repo)
            log.info("fetched %s (%s) in %.1fs", repo.name, repo.default_branch, time.time() - t)
        except Exception as e:
            failed.append(repo.name)
            log.error("fetch failed for %s: %s", repo.name, e)
    return failed


def snapshots(settings: Settings, local_path: Path | None) -> list[RepoSnapshot]:
    if local_path:
        return LocalRepo.open_all(local_path)
    return GitMirror.open_all(settings.mirrors_dir)


def build(snaps: list[RepoSnapshot], cache: ParseCache) -> Graph:
    parsed = []
    for s in snaps:
        t = time.time()
        repo, stats = parse_repo(s, cache)
        parsed.append(repo)
        log.info("parsed %-40s %s in %.1fs", s.name, stats, time.time() - t)
    t = time.time()
    g = build_graph(parsed)
    log.info("built graph: %d nodes, %d edges in %.1fs", len(g.nodes), len(g.edges), time.time() - t)
    return g


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
