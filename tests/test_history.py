import re
from datetime import date

import pytest
from conftest import FIND, PLACE, SVC, make_history_repo

from kg.history import (HistoryGraph, changed_work_item_ids, commit_id, commits_with_spans, html_to_text, map_ranges,
                        parse_git_log, utc_iso, wi_refs)
from kg.pipeline import ParseCache

FILE = f"file:order-service:{SVC}/OrderServiceImpl.java"


def _edges(h, type_, src=None, dst=None):
    return {(e.src, e.dst): e.props for e in h.g.edges.values()
            if e.type == type_ and (src is None or e.src == src) and (dst is None or e.dst == dst)}


def test_commits_map_hunks_to_methods(tmp_path):
    mirror, shas = make_history_repo(tmp_path)
    h = HistoryGraph()
    n = commits_with_spans(mirror, ParseCache(None), h, since=None, exclude=None)
    assert n == 4  # first-parent: the feature branch commit itself is not listed, its merge is

    c2, merge, c4 = (commit_id("order-service", shas[k]) for k in ("c2", "merge", "c4"))
    assert h.g.nodes[c2].props["name"] == "Fix total in place AB#4321"
    assert h.g.nodes[c2].props["email"] == "dev@acme.test"
    # c2 edited one line inside place(): method + file, nothing else
    mod = _edges(h, "MODIFIED", src=c2)
    assert set(mod) == {(c2, PLACE), (c2, FILE)}
    assert mod[(c2, FILE)]["added"] == 1 and mod[(c2, FILE)]["deleted"] == 1
    assert _edges(h, "RESOLVES", src=c2)[(c2, "wi:4321")]["via"] == "message"
    assert 4321 in h.wi_referenced
    # the merge carries the branch's change (diff against the first parent) and links to its PR
    assert (merge, FIND) in _edges(h, "MODIFIED", src=merge)
    assert (merge, PLACE) not in _edges(h, "MODIFIED", src=merge)
    assert ("pr:77", merge) in _edges(h, "MERGED_AS")
    # a new file maps to its method; README.md is not an indexed file kind
    extra = _edges(h, "MODIFIED", src=c4)
    assert (c4, "java:order-service:com.acme.order.service.Extra#x()") in extra
    assert not any(dst.endswith("README.md") for _, dst in extra)
    assert ("person:dev@acme.test", c2) in _edges(h, "AUTHORED")


def test_incremental_log_only_returns_new_commits(tmp_path):
    mirror, shas = make_history_repo(tmp_path)
    h = HistoryGraph()
    assert commits_with_spans(mirror, ParseCache(None), h, since=None, exclude=shas["c2"]) == 2
    assert {n.props["sha"] for n in h.g.nodes.values() if n.label == "Commit"} == {shas["merge"], shas["c4"]}


def test_parse_git_log_edge_cases():
    raw = (
        "\x1eabc\x1fp1\x1fA\x1fA@X.COM\x1f2025-01-02T03:04:05+03:00\x1fSubject\n\nBody line\n\x1d\n"
        "\n"
        ":100644 100644 aaa bbb M\tsrc/my dir/A.java\n"
        ":100644 000000 ccc 0000000000000000000000000000000000000000 D\tsrc/Gone.java\n"
        ":100644 100644 ddd eee M\tdocs/notes.txt\n"
        "\n"
        "diff --git a/src/my dir/A.java b/src/my dir/A.java\n"
        "index aaa..bbb 100644\n"
        "--- a/src/my dir/A.java\t\n"
        "+++ b/src/my dir/A.java\t\n"
        "@@ -10,2 +9,0 @@ class A {\n"
        "-    int x;\n"
        "-    int y;\n"
        "@@ -20 +18,2 @@\n"
        "-@@ not a hunk header, just content\n"
        "+diff --git looks like a header but is content\n"
        "+}\n"
        "\\ No newline at end of file\n"
        "diff --git a/src/Gone.java b/src/Gone.java\n"
        "deleted file mode 100644\n"
        "--- a/src/Gone.java\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-a\n"
        "-b\n"
        "-c\n"
        "diff --git a/docs/notes.txt b/docs/notes.txt\n"
        "--- a/docs/notes.txt\n"
        "+++ b/docs/notes.txt\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
        "\x1edef\x1fabc\x1fB\x1fb@x.com\x1f2025-01-03T00:00:00Z\x1fSecond\x1d\n"
    )
    # split like the byte stream from git (only on \n); str.splitlines() would also split on \x1d/\x1e
    first, second = list(parse_git_log(line + "\n" for line in raw.split("\n")))
    assert (first.sha, first.parents, first.email, first.date) == ("abc", ["p1"], "a@x.com", "2025-01-02T00:04:05Z")
    assert first.message == "Subject\n\nBody line"
    a = first.changes["src/my dir/A.java"]
    assert a.blob == "bbb" and a.ranges == [(9, 9), (18, 19)] and (a.added, a.deleted) == (2, 3)
    gone = first.changes["src/Gone.java"]
    assert gone.status == "D" and gone.blob is None and gone.deleted == 3
    assert "docs/notes.txt" not in first.changes  # not an indexed kind
    assert second.sha == "def" and second.changes == {}


def test_map_ranges():
    spans = [("C", 1, 50, False), ("C#a()", 5, 10, True), ("C#b()", 12, 20, True),
             ("C.Inner", 30, 45, False), ("C.Inner#c()", 32, 35, True)]
    assert map_ranges(spans, [(6, 6)]) == {"C#a()": 1}
    assert map_ranges(spans, [(9, 13)]) == {"C#a()": 1, "C#b()": 1}
    assert map_ranges(spans, [(40, 41)]) == {"C.Inner": 1}   # innermost class, not C
    assert map_ranges(spans, [(2, 2), (7, 7)]) == {"C": 1, "C#a()": 1}
    assert map_ranges(spans, [(60, 61)]) == {}


def test_pull_request_and_work_item_conversion():
    h = HistoryGraph()
    h.add_pull_request("order-service", {
        "pullRequestId": 77, "title": "Login fix for AB#9001", "description": "<p>details</p>", "status": "completed",
        "sourceRefName": "refs/heads/feature/12345-login", "targetRefName": "refs/heads/main",
        "creationDate": "2025-03-01T10:00:00.1234567Z", "closedDate": "2025-03-02T10:00:00Z",
        "createdBy": {"displayName": "Ayşe"}, "lastMergeCommit": {"commitId": "ABCDEF"}})
    pr = h.g.nodes["pr:77"].props
    assert pr["source_branch"] == "feature/12345-login" and pr["created"] == "2025-03-01T10:00:00Z"
    assert ("pr:77", "commit:order-service:abcdef") in _edges(h, "MERGED_AS")
    res = _edges(h, "RESOLVES", src="pr:77")
    assert res[("pr:77", "wi:12345")] == {"via": "branch", "confidence": 0.6}
    assert res[("pr:77", "wi:9001")]["via"] == "message"

    linked = h.add_work_item({"id": 4321, "fields": {
        "System.WorkItemType": "Bug", "System.Title": "Total is wrong", "System.State": "Done",
        "System.Tags": "billing; urgent", "System.AssignedTo": {"displayName": "Mehmet"},
        "System.ChangedDate": "2025-04-01T08:00:00Z",
        "Microsoft.VSTS.TCM.ReproSteps": "<div>1. Open<br>2. Pay</div>"},
        "relations": [
            {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": "https://tfs/_apis/wit/workItems/4000"},
            {"rel": "ArtifactLink", "url": "vstfs:///Git/Commit/PROJ%2FREPO-GUID-1%2Fabc123"},
            {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/PROJ%2FREPO-GUID-1%2F77"},
            {"rel": "ArtifactLink", "url": "vstfs:///Git/Commit/PROJ%2FUNKNOWN-REPO%2Fdef"},
        ]}, {"repo-guid-1": "order-service"})
    assert linked == {4000}
    wi = h.g.nodes["wi:4321"]
    assert wi.extra_labels == {"Bug"} and wi.props["tags"] == ["billing", "urgent"]
    assert wi.props["assigned_to"] == "Mehmet" and wi.props["repro_steps"] == "1. Open\n2. Pay"
    assert ("wi:4321", "wi:4000") in _edges(h, "CHILD_OF")
    res = _edges(h, "RESOLVES", dst="wi:4321")
    assert set(res) == {("commit:order-service:abc123", "wi:4321"), ("pr:77", "wi:4321")}
    assert all(p["confidence"] == 1.0 for p in res.values())


def test_text_helpers():
    assert html_to_text("<ul><li>a &amp; b</li><li>c</li></ul>") == "- a & b\n- c"
    assert html_to_text(None) == ""
    assert wi_refs("Fix AB#123 and #4567, not &#8217; or #1") == {123, 4567}
    assert utc_iso("2025-01-02T03:04:05+03:00") == "2025-01-02T00:04:05Z"


class FakeAdo:
    def __init__(self, max_days=10, fail=None):
        self.calls, self.wheres, self.max_days, self.fail = [], [], max_days, fail

    def wiql_ids(self, where):
        if self.fail:
            raise self.fail
        a, b = (date.fromisoformat(x) for x in re.findall(r"'(\d{4}-\d\d-\d\d)'", where)[:2])
        self.calls.append((a, b))
        self.wheres.append(where)
        if (b - a).days > self.max_days:
            raise RuntimeError("VS402337: more than 20000 results")
        return [a.toordinal()]


def test_wiql_windows_split_when_too_large():
    ado = FakeAdo()
    ids = changed_work_item_ids(ado, ["Bug", "Product Backlog Item"], date(2025, 1, 1), date(2025, 2, 1))
    assert "[System.WorkItemType] IN ('Bug','Product Backlog Item')" in ado.wheres[0]
    assert len(ids) >= 3 and ids == sorted(set(ids))
    ok = [c for c in ado.calls if (c[1] - c[0]).days <= 10]
    # the successful windows tile the whole range without gaps
    assert ok[0][0] == date(2025, 1, 1) and ok[-1][1] == date(2025, 2, 1)
    assert all(x[1] == y[0] for x, y in zip(ok, ok[1:]))


def test_wiql_other_errors_are_not_split():
    with pytest.raises(ConnectionError):
        changed_work_item_ids(FakeAdo(fail=ConnectionError("down")), [], date(2025, 1, 1), date(2025, 2, 1))
