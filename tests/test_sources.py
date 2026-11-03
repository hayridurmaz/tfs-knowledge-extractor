import shutil
import subprocess

from conftest import FIXTURES

from kg.pipeline import ParseCache, parse_repo
from kg.sources.repos import GitMirror, LocalRepo, file_kind


def test_file_kind():
    assert file_kind("src/main/java/A.java") == "java"
    assert file_kind("web/src/App.jsx") == "tsx"
    assert file_kind("web/src/util.js") == "tsx"
    assert file_kind("web/src/util.ts") == "ts"
    assert file_kind("web/node_modules/x/index.js") is None
    assert file_kind("web/src/types.d.ts") is None
    assert file_kind("svc/target/generated-sources/A.java") is None
    assert file_kind("svc/src/main/resources/application-prod.yml") == "spring_config"
    assert file_kind("svc/src/main/resources/logback.xml") is None


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_git_mirror_reads_default_branch(tmp_path):
    work = tmp_path / "work"
    shutil.copytree(FIXTURES / "order-service", work)
    _git("init", "-q", "-b", "main", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=work)
    bare = tmp_path / "mirrors" / "order-service"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    (bare / GitMirror.META).write_text('{"name": "order-service", "default_branch": "main"}')

    [m] = GitMirror.open_all(tmp_path / "mirrors")
    files = {f.path for f in m.files()}
    assert "src/main/java/com/acme/order/api/OrderController.java" in files and "pom.xml" in files
    local = LocalRepo(FIXTURES / "order-service")
    assert files == {f.path for f in local.files()}
    # identical content -> the git mirror parse equals the folder parse
    cache = ParseCache(None)
    a, stats_a = parse_repo(m, cache)
    b, _ = parse_repo(local, ParseCache(None))
    assert stats_a["failed"] == 0
    assert {f.path: f.data for f in a.files} == {f.path: f.data for f in b.files}
    # second run is served from cache
    _, stats_again = parse_repo(m, cache)
    assert stats_again["parsed"] == 0 and stats_again["cached"] == stats_a["files"]
