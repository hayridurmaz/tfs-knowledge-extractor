from pathlib import Path

import pytest

from kg.build import build_graph
from kg.config import Settings
from kg.pipeline import ParseCache, parse_repo, snapshots

FIXTURES = Path(__file__).parent / "fixtures" / "repos"


@pytest.fixture(scope="session")
def graph():
    parsed = [parse_repo(s, ParseCache(None))[0] for s in snapshots(Settings(), FIXTURES)]
    return build_graph(parsed)


def edges(graph, type_, src=None, dst=None):
    return [e for e in graph.edges.values()
            if e.type == type_ and (src is None or e.src == src) and (dst is None or e.dst == dst)]


SVC = "src/main/java/com/acme/order/service"
PLACE = "java:order-service:com.acme.order.service.OrderServiceImpl#place(Order)"
FIND = "java:order-service:com.acme.order.service.OrderServiceImpl#find(Long)"


def make_history_repo(tmp_path: Path):
    """A bare mirror of order-service with history:
    c1 init; c2 edits place() ("AB#4321"); merge of a branch that edits find() ("Merged PR 77");
    c4 adds Extra.java and a README. Returns (GitMirror, {name: sha})."""
    import os
    import shutil
    import subprocess

    from kg.sources.repos import GitMirror

    work = tmp_path / "work"
    shutil.copytree(FIXTURES / "order-service", work)
    impl = work / SVC / "OrderServiceImpl.java"

    day = iter(range(1, 28))

    def git(*args):
        # each commit gets its own day, so "newest first" ordering is deterministic
        when = f"2025-01-{next(day):02d}T10:00:00+00:00" if args[0] in ("commit", "merge") else None
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
        return subprocess.run(["git", "-c", "user.email=dev@acme.test", "-c", "user.name=Dev", "-c",
                               "core.autocrlf=false", *args], cwd=work, check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def edit(old, new):
        text = impl.read_text()
        assert old in text
        impl.write_text(text.replace(old, new))

    shas = {}
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-qm", "init")
    shas["c1"] = git("rev-parse", "HEAD")
    edit("        Money total = zero();", "        Money total = zero().plus(Money.of(1));")
    git("commit", "-qam", "Fix total in place AB#4321")
    shas["c2"] = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "feature/5555-find")
    edit("return orderRepository.findById(id).orElseThrow();",
         "return orderRepository.findById(id).orElseThrow(() -> new IllegalStateException(\"missing\"));")
    git("commit", "-qam", "Better error in find")
    git("checkout", "-q", "main")
    git("merge", "--no-ff", "-q", "-m", "Merged PR 77: better error in find", "feature/5555-find")
    shas["merge"] = git("rev-parse", "HEAD")
    (work / SVC / "Extra.java").write_text("package com.acme.order.service;\n\npublic class Extra {\n"
                                           "    void x() {\n        System.out.println(1);\n    }\n}\n")
    (work / "README.md").write_text("docs\n")
    git("add", ".")
    git("commit", "-qm", "Add Extra")
    shas["c4"] = git("rev-parse", "HEAD")

    bare = tmp_path / "mirrors" / "order-service"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    (bare / GitMirror.META).write_text('{"name": "order-service", "default_branch": "main", "id": "REPO-GUID-1"}')
    [mirror] = GitMirror.open_all(tmp_path / "mirrors")
    return mirror, shas
