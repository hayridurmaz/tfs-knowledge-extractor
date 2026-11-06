"""Phase 2: commit, pull request and work item history, linked to the code graph.

Commits come from the local bare mirrors (`git log --first-parent` of the default branch). Each diff hunk is mapped
onto the methods/functions of the file *at that commit*: the post-image blob is parsed with the same parsers as the
code graph (through the parse cache, so unchanged blobs are never re-parsed) and node ids are derived the same way,
so a commit links to today's Method node as long as the method still exists. Every change also gets a File-level
edge, which survives renames/removals of the method.

Pull requests and work items come from the Azure DevOps REST API. History facts carry `src='history'`, so the
code sweep never deletes them; they accumulate run over run, driven by watermarks in (:IngestState {id:'history'}).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

from kg.build import file_id, symbol_spans
from kg.config import Settings
from kg.graph import Graph, tokens
from kg.pipeline import ParseCache, parse_file
from kg.sources.repos import GitMirror, SourceFile, file_kind

log = logging.getLogger(__name__)

SEP_COMMIT, SEP_FIELD, SEP_END = "\x1e", "\x1f", "\x1d"
LOG_FORMAT = "%x1e%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%B%x1d"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MAPPED_KINDS = ("java", "ts", "tsx")
CHUNK_COMMITS = 300  # commits per blob-parsing batch (bounds memory)
MAX_TEXT = 20_000

# Work item references in commit messages / PR titles: "AB#123", "#123" (not HTML entities like "&#123;").
_WI_REF = re.compile(r"(?<![\w&])(?:AB)?#(\d{2,8})\b")
# Azure DevOps default merge message: "Merged PR 1234: title"
_MERGED_PR = re.compile(r"^Merged PR (\d+)", re.MULTILINE)
# Work item id in a branch name: feature/12345-foo, bugfix/12345, users/x/12345_fix
_BRANCH_WI = re.compile(r"(?:^|/)(\d{3,8})(?=$|[-_/.])")

WORK_ITEM_LABELS = {"Bug": "Bug", "Product Backlog Item": "PBI", "User Story": "UserStory", "Feature": "Feature",
                    "Epic": "Epic", "Task": "Task", "Issue": "Issue"}


# ---------------------------------------------------------------- git log parsing

@dataclass
class FileChange:
    path: str
    status: str = "M"          # A, M, D, T (renames are split into D + A by --no-renames)
    blob: str | None = None    # post-image blob sha, None when deleted
    ranges: list[tuple[int, int]] = field(default_factory=list)  # new-file line ranges touched by hunks
    added: int = 0
    deleted: int = 0


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author: str
    email: str
    date: str                  # UTC ISO-8601, sortable
    message: str
    changes: dict[str, FileChange] = field(default_factory=dict)


def utc_iso(value: str) -> str:
    """'2025-01-02T03:04:05+03:00' / '...Z' / '....123Z' -> '2025-01-02T00:04:05Z'."""
    v = value.strip().replace("Z", "+00:00")
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)  # ADO sends 7 fractional digits
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return value
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_log_cmd(branch: str, since: str | None = None, exclude: str | None = None) -> list[str]:
    cmd = ["git", "-c", "core.quotePath=false", "log", f"refs/heads/{branch}", "--first-parent",
           "--diff-merges=first-parent", "--no-renames", "--no-color", "--no-ext-diff", "--raw", "--no-abbrev",
           "-p", "-U0", f"--format={LOG_FORMAT}"]
    if since:
        cmd.append(f"--since={since}")
    if exclude:
        cmd.append(f"^{exclude}")
    return cmd


def parse_git_log(lines: Iterator[str]) -> Iterator[Commit]:
    """Parse `git log --raw -p -U0` output in LOG_FORMAT. Only indexed file kinds are kept."""
    cur: Commit | None = None
    header: list[str] | None = None
    fc: FileChange | None = None
    old_path: str | None = None
    skip = 0
    for raw in lines:
        line = raw.rstrip("\n")
        if skip:
            if not line.startswith("\\"):  # "\ No newline at end of file" is not a counted hunk line
                skip -= 1
            continue
        if header is not None or line.startswith(SEP_COMMIT):
            if line.startswith(SEP_COMMIT):
                if cur:
                    yield cur
                cur, fc, header = None, None, []
                line = line[1:]
            header.append(line)
            if SEP_END in line:
                text = "\n".join(header).split(SEP_END, 1)[0]
                sha, parents, author, email, when, message = (text.split(SEP_FIELD) + [""] * 6)[:6]
                cur = Commit(sha, parents.split(), author, email.lower(), utc_iso(when), message.strip())
                header = None
            continue
        if cur is None:
            continue
        if line.startswith(":"):  # raw: ":100644 100644 <old> <new> M\tpath"
            meta, _, path = line.partition("\t")
            parts = meta[1:].split()
            if len(parts) >= 5 and file_kind(path):
                status = parts[4][0]
                cur.changes[path] = FileChange(path, status, None if status == "D" else parts[3])
        elif line.startswith("diff --git "):
            fc, old_path = None, None
        elif line.startswith("--- "):  # git appends a tab to names that contain spaces
            old_path = line[6:].rstrip("\t") if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            path = line[6:].rstrip("\t") if line.startswith("+++ b/") else old_path
            fc = cur.changes.get(path) if path else None
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                continue
            b = 1 if m.group(2) is None else int(m.group(2))
            c = int(m.group(3))
            d = 1 if m.group(4) is None else int(m.group(4))
            skip = b + d
            if fc is not None:
                fc.added += d
                fc.deleted += b
                start = max(c, 1)
                fc.ranges.append((start, c + d - 1 if d > 0 else start))
    if cur:
        yield cur


def git_log(mirror: GitMirror, since: str | None = None, exclude: str | None = None) -> Iterator[Commit]:
    proc = subprocess.Popen(git_log_cmd(mirror.default_branch, since, exclude), cwd=mirror.path,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        lines = (b.decode("utf-8", errors="replace") for b in proc.stdout)
        yield from parse_git_log(lines)
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.wait() != 0:
            raise RuntimeError(f"git log failed in {mirror.name}: {err}")


def is_ancestor(mirror: GitMirror, sha: str) -> bool:
    r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, mirror.head_sha], cwd=mirror.path,
                       capture_output=True)
    return r.returncode == 0


# ---------------------------------------------------------------- hunk -> symbol mapping

def map_ranges(spans: list[tuple[str, int, int, bool]], ranges: list[tuple[int, int]]) -> dict[str, int]:
    """Symbol ids touched by the given line ranges -> number of hunks. A hunk maps to the methods/functions it
    overlaps; a hunk outside any member (fields, imports inside a class) maps to the innermost class."""
    out: dict[str, int] = {}
    for s, e in ranges:
        hits = [sid for sid, a, b, member in spans if member and s <= b and e >= a]
        if not hits:
            boxes = [(b - a, sid) for sid, a, b, member in spans if not member and s <= b and e >= a]
            hits = [min(boxes)[1]] if boxes else []
        for sid in hits:
            out[sid] = out.get(sid, 0) + 1
    return out


class SpanResolver:
    """Symbol spans of (path, blob) pairs, parsed through the shared parse cache."""

    def __init__(self, mirror: GitMirror, cache: ParseCache) -> None:
        self.mirror, self.cache = mirror, cache

    def spans(self, wanted: list[SourceFile]) -> dict[tuple[str, str], list[tuple[str, int, int, bool]]]:
        uniq = {(f.path, f.blob_id): f for f in wanted}
        keys = {k: ParseCache.key(f) for k, f in uniq.items()}
        cached = self.cache.get_many(list(set(keys.values())))
        out: dict[tuple[str, str], list] = {}
        todo = []
        for k, f in uniq.items():
            data = cached.get(keys[k])
            if data is None:
                todo.append(f)
            else:
                out[k] = symbol_spans(self.mirror.name, f.path, f.kind, data)
        for f, raw in self.mirror.read_many(todo):
            try:
                data = parse_file(f, raw)
            except Exception as e:  # a broken historical file only loses method-level detail
                log.debug("%s: cannot parse %s@%s: %s", self.mirror.name, f.path, f.blob_id[:8], e)
                continue
            self.cache.put(ParseCache.key(f), data)
            out[(f.path, f.blob_id)] = symbol_spans(self.mirror.name, f.path, f.kind, data)
        self.cache.commit()
        return out


# ---------------------------------------------------------------- graph building

def commit_id(repo: str, sha: str) -> str:
    return f"commit:{repo}:{sha.lower()}"


def wi_id(n: int | str) -> str:
    return f"wi:{int(n)}"


def pr_id(n: int | str) -> str:
    return f"pr:{int(n)}"


def first_line(text: str, limit: int = 200) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


def wi_refs(text: str) -> set[int]:
    return {int(x) for x in _WI_REF.findall(text or "")}


class HistoryGraph:
    """Collects history nodes and edges. Edges may point at code nodes that exist only in Neo4j."""

    def __init__(self) -> None:
        self.g = Graph()
        self.wi_referenced: set[int] = set()
        self.wi_fetched: set[int] = set()

    # ---- commits
    def add_commits(self, repo: str, commits: list[Commit], spans: dict[tuple[str, str], list]) -> None:
        g = self.g
        for c in commits:
            cid = commit_id(repo, c.sha)
            g.node(cid, "Commit", name=first_line(c.message), message=c.message[:MAX_TEXT], doc=c.message[:MAX_TEXT],
                   sha=c.sha, short=c.sha[:8], repo=repo, author=c.author, email=c.email, date=c.date,
                   is_merge=len(c.parents) > 1, files_changed=len(c.changes), tokens=tokens(first_line(c.message)))
            if c.email:
                pid = f"person:{c.email}"
                g.node(pid, "Person", name=c.author, email=c.email)
                g.edge("AUTHORED", pid, cid)
            for fc in c.changes.values():
                g.edge("MODIFIED", cid, file_id(repo, fc.path), added=fc.added, deleted=fc.deleted, status=fc.status)
                if fc.blob and (fc.path, fc.blob) in spans:
                    for sid, hunks in map_ranges(spans[(fc.path, fc.blob)], fc.ranges).items():
                        g.edge("MODIFIED", cid, sid, hunks=hunks)
            for n in wi_refs(c.message):
                self.wi_referenced.add(n)
                g.edge("RESOLVES", cid, wi_id(n), via="message", confidence=0.8)
            for n in _MERGED_PR.findall(c.message):
                g.edge("MERGED_AS", pr_id(n), cid)

    # ---- pull requests
    def add_pull_request(self, repo: str, pr: dict) -> None:
        g = self.g
        pid = pr_id(pr["pullRequestId"])
        title, desc = pr.get("title") or "", (pr.get("description") or "")[:MAX_TEXT]
        src = (pr.get("sourceRefName") or "").removeprefix("refs/heads/")
        g.node(pid, "PullRequest", name=title[:200], title=title, doc=desc or None, number=pr["pullRequestId"],
               repo=repo, status=pr.get("status"), source_branch=src,
               target_branch=(pr.get("targetRefName") or "").removeprefix("refs/heads/"),
               author=(pr.get("createdBy") or {}).get("displayName"),
               created=utc_iso(pr["creationDate"]) if pr.get("creationDate") else None,
               closed=utc_iso(pr["closedDate"]) if pr.get("closedDate") else None,
               merge_commit=(pr.get("lastMergeCommit") or {}).get("commitId"), tokens=tokens(title))
        merge = (pr.get("lastMergeCommit") or {}).get("commitId")
        if merge:
            g.edge("MERGED_AS", pid, commit_id(repo, merge))
        for n in wi_refs(f"{title}\n{desc}"):
            self.wi_referenced.add(n)
            g.edge("RESOLVES", pid, wi_id(n), via="message", confidence=0.8)
        for n in _BRANCH_WI.findall(src):
            self.wi_referenced.add(int(n))
            g.edge("RESOLVES", pid, wi_id(n), via="branch", confidence=0.6)

    # ---- work items
    def add_work_item(self, w: dict, repo_names: dict[str, str]) -> set[int]:
        """Adds one work item; returns ids of linked work items (parents) for a follow-up fetch."""
        g, f = self.g, w.get("fields", {})
        n = int(w["id"])
        self.wi_fetched.add(n)
        typ = f.get("System.WorkItemType", "")
        title = f.get("System.Title", "")
        assigned = f.get("System.AssignedTo")
        if isinstance(assigned, dict):
            assigned = assigned.get("displayName")
        texts = {k: html_to_text(f.get(fld)) for k, fld in (
            ("description", "System.Description"), ("acceptance_criteria", "Microsoft.VSTS.Common.AcceptanceCriteria"),
            ("repro_steps", "Microsoft.VSTS.TCM.ReproSteps"))}
        doc = "\n\n".join(v for v in texts.values() if v)[:MAX_TEXT]
        label = WORK_ITEM_LABELS.get(typ)
        g.node(wi_id(n), "WorkItem", {label} if label else None, name=title[:200], title=title, number=n, type=typ,
               state=f.get("System.State"), reason=f.get("System.Reason"), area=f.get("System.AreaPath"),
               iteration=f.get("System.IterationPath"),
               tags=[t.strip() for t in (f.get("System.Tags") or "").split(";") if t.strip()],
               priority=f.get("Microsoft.VSTS.Common.Priority"), severity=f.get("Microsoft.VSTS.Common.Severity"),
               assigned_to=assigned,
               created=utc_iso(f["System.CreatedDate"]) if f.get("System.CreatedDate") else None,
               changed=utc_iso(f["System.ChangedDate"]) if f.get("System.ChangedDate") else None,
               **{k: v[:MAX_TEXT] for k, v in texts.items() if v}, doc=doc or None, tokens=tokens(title))
        linked: set[int] = set()
        for rel in w.get("relations") or []:
            kind, url = rel.get("rel", ""), rel.get("url", "")
            if kind == "System.LinkTypes.Hierarchy-Reverse":
                parent = int(url.rstrip("/").rpartition("/")[2])
                linked.add(parent)
                g.edge("CHILD_OF", wi_id(n), wi_id(parent))
            elif kind == "ArtifactLink":
                art = _artifact(url)
                if not art:
                    continue
                typ_, repo_id, ref = art
                repo = repo_names.get(repo_id.lower())
                if typ_ == "Commit" and repo:
                    g.edge("RESOLVES", commit_id(repo, ref), wi_id(n), via="link", confidence=1.0)
                elif typ_ == "PullRequestId":
                    g.edge("RESOLVES", pr_id(ref), wi_id(n), via="link", confidence=1.0)
        return linked


def _artifact(url: str) -> tuple[str, str, str] | None:
    """vstfs:///Git/Commit/{project}%2F{repoId}%2F{sha} -> ('Commit', repoId, sha); same for PullRequestId."""
    m = re.match(r"vstfs:///Git/(Commit|PullRequestId)/(.+)$", url, re.IGNORECASE)
    if not m:
        return None
    parts = re.split(r"%2f|/", m.group(2), flags=re.IGNORECASE)
    if len(parts) != 3:
        return None
    return m.group(1), parts[1], parts[2]


class _Text(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    p = _Text()
    p.feed(value)
    p.close()
    text = "".join(p.out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- orchestration

def commits_with_spans(mirror: GitMirror, cache: ParseCache, h: HistoryGraph, since: str | None,
                       exclude: str | None) -> int:
    resolver = SpanResolver(mirror, cache)
    total = 0
    batch: list[Commit] = []

    def flush() -> None:
        wanted = [SourceFile(fc.path, file_kind(fc.path), fc.blob) for c in batch for fc in c.changes.values()
                  if fc.blob and fc.ranges and file_kind(fc.path) in MAPPED_KINDS]
        h.add_commits(mirror.name, batch, resolver.spans(wanted))
        batch.clear()

    for c in git_log(mirror, since, exclude):
        batch.append(c)
        total += 1
        if len(batch) >= CHUNK_COMMITS:
            flush()
    if batch:
        flush()
    return total


def _months_ago(months: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=30 * months)).date()


def _windows(start: date, end: date, days: int = 31) -> Iterator[tuple[date, date]]:
    d = start
    while d < end:
        nxt = min(d + timedelta(days=days), end)
        yield d, nxt
        d = nxt


def _too_many_results(e: Exception) -> bool:
    """WIQL over the 20k cap fails with HTTP 400 and error VS402337."""
    resp = getattr(e, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 400:
        return True
    return "VS402337" in str(e)


def changed_work_item_ids(ado, types: list[str], start: date, end: date) -> list[int]:
    """WIQL in date windows; a window over the server's 20k result cap is split in half."""
    type_clause = ""
    if types:
        quoted = ",".join("'" + t.replace("'", "''") + "'" for t in types)
        type_clause = f" AND [System.WorkItemType] IN ({quoted})"
    ids: list[int] = []
    stack = list(_windows(start, end))[::-1]
    while stack:
        a, b = stack.pop()
        where = f"[System.ChangedDate] >= '{a.isoformat()}' AND [System.ChangedDate] < '{b.isoformat()}'{type_clause}"
        try:
            ids.extend(ado.wiql_ids(where))
        except Exception as e:
            if not _too_many_results(e) or (b - a).days <= 1:
                raise
            log.info("WIQL window %s..%s too large (%s), splitting", a, b, e)
            mid = a + (b - a) / 2
            stack += [(mid, b), (a, mid)]
    return sorted(set(ids))


def run_history(settings: Settings, only: set[str] | None = None, skip_prs: bool = False,
                skip_work_items: bool = False) -> dict:
    from kg.sources.ado import AdoClient
    from kg.store import Store

    t0 = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start = _months_ago(settings.history_months)
    st = Store(settings)
    stats: dict = {"commits": 0, "pull_requests": 0, "work_items": 0, "repos_failed": []}
    try:
        st.init_schema()
        state = st.history_state()
        last_shas: dict[str, str] = dict(state.get("commits") or {})
        h = HistoryGraph()
        cache = ParseCache(settings.cache_path)
        mirrors = [m for m in GitMirror.open_all(settings.mirrors_dir) if not only or m.name in only]
        repo_names = {}
        for m in mirrors:
            meta = json.loads((Path(m.path) / GitMirror.META).read_text())
            if meta.get("id"):
                repo_names[meta["id"].lower()] = m.name

        # 1. commits (local mirrors)
        for m in mirrors:
            t = time.time()
            last = last_shas.get(m.name)
            exclude = last if last and is_ancestor(m, last) else None
            try:
                n = commits_with_spans(m, cache, h, None if exclude else window_start.isoformat(), exclude)
            except Exception as e:
                stats["repos_failed"].append(m.name)
                log.error("history: git log failed for %s: %s", m.name, e)
                continue
            last_shas[m.name] = m.head_sha
            stats["commits"] += n
            log.info("history: %-40s %5d new commits in %.1fs", m.name, n, time.time() - t)

        ado = AdoClient(settings) if settings.ado_url and not (skip_prs and skip_work_items) else None

        # 2. pull requests (ADO): re-read a margin before the last run, PRs are merged idempotently
        if ado and not skip_prs:
            since = state.get("prs_after")
            created_after = (datetime.fromisoformat(since.replace("Z", "+00:00")).date() - timedelta(days=60)
                             if since else window_start).isoformat()
            for repo_id, name in repo_names.items():
                try:
                    for pr in ado.pull_requests(repo_id, created_after):
                        h.add_pull_request(name, pr)
                        stats["pull_requests"] += 1
                except Exception as e:
                    stats["repos_failed"].append(name)
                    log.error("history: pull requests failed for %s: %s", name, e)
            log.info("history: %d pull requests", stats["pull_requests"])

        # 3. work items (ADO): changed in the window, plus anything history refers to, plus their parents
        wi_changed = state.get("wi_changed")
        if ado and not skip_work_items:
            start = (datetime.fromisoformat(wi_changed.replace("Z", "+00:00")).date() - timedelta(days=1)
                     if wi_changed else window_start)
            ids = changed_work_item_ids(ado, settings.work_item_types, start, date.today() + timedelta(days=1))
            log.info("history: %d work items changed since %s", len(ids), start)
            pending = set(ids)
            for _round in range(3):
                pending |= h.wi_referenced
                pending -= h.wi_fetched
                if not pending:
                    break
                linked: set[int] = set()
                for w in ado.work_items(sorted(pending)):
                    linked |= h.add_work_item(w, repo_names)
                    changed = (w.get("fields") or {}).get("System.ChangedDate")
                    if changed and (not wi_changed or utc_iso(changed) > wi_changed):
                        wi_changed = utc_iso(changed)
                h.wi_fetched |= pending  # ids the server omitted (deleted, other project) are not retried
                pending = linked
            stats["work_items"] = len([n for n in h.g.nodes.values() if n.label == "WorkItem"])

        w = st.write_history(h.g)
        st.save_history_state({"commits": last_shas, "prs_after": started if ado and not skip_prs
                               else state.get("prs_after"), "wi_changed": wi_changed})
        stats.update(w)
    finally:
        st.close()
    stats["seconds"] = round(time.time() - t0, 1)
    log.info("history finished: %s", stats)
    return stats
