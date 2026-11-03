"""Read repository snapshots from bare git mirrors (production) or plain folders (development)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from kg.config import Settings
from kg.sources.ado import AdoRepo, basic_auth_header

log = logging.getLogger(__name__)

SKIP_DIRS = {"node_modules", "target", "build", "dist", ".git", ".idea", ".vscode", "out",
             "coverage", ".next", ".gradle", "bin", "obj", "generated", "generated-sources"}
MAX_FILE_BYTES = 1_000_000

SOURCE_EXT = {".java", ".ts", ".tsx", ".js", ".jsx"}
CONFIG_NAMES = ("application", "bootstrap")


def file_kind(path: str) -> str | None:
    """Classify a repo-relative path into a parser kind, or None to skip it."""
    p = path.replace("\\", "/")
    parts = p.split("/")
    if any(d in SKIP_DIRS for d in parts[:-1]):
        return None
    name = parts[-1]
    lower = name.lower()
    if lower.endswith(".d.ts") or ".min." in lower:
        return None
    ext = os.path.splitext(lower)[1]
    if ext == ".java":
        return "java"
    if ext == ".ts":
        return "ts"
    if ext in (".tsx", ".jsx", ".js"):  # React .js files often contain JSX
        return "tsx"
    if name == "pom.xml":
        return "pom"
    if name == "package.json":
        return "package_json"
    if name in ("tsconfig.json", "jsconfig.json"):
        return "tsconfig"
    if lower.startswith(CONFIG_NAMES) and ext in (".yml", ".yaml", ".properties"):
        return "spring_config"
    return None


@dataclass(frozen=True)
class SourceFile:
    path: str
    kind: str
    blob_id: str  # content hash used as the parse-cache key


class RepoSnapshot(Protocol):
    name: str
    default_branch: str
    head_sha: str

    def files(self) -> list[SourceFile]: ...
    def read_many(self, files: list[SourceFile]) -> Iterator[tuple[SourceFile, bytes]]: ...


# ---------------------------------------------------------------- git mirrors

def _git(args: list[str], cwd: Path | None = None, extra_config: list[str] | None = None,
         input: bytes | None = None) -> bytes:
    cmd = ["git"]
    for c in extra_config or []:
        cmd += ["-c", c]
    cmd += args
    r = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


class GitMirror:
    """A bare mirror holding only the default branch of one ADO repo."""

    META = "kg-meta.json"

    def __init__(self, path: Path, name: str, default_branch: str) -> None:
        self.path = path
        self.name = name
        self.default_branch = default_branch
        self.head_sha = _git(["rev-parse", f"refs/heads/{default_branch}"], cwd=path).decode().strip()

    @classmethod
    def fetch(cls, settings: Settings, repo: AdoRepo) -> "GitMirror":
        path = settings.mirrors_dir / repo.name
        cfg = [f"http.extraHeader=Authorization: {basic_auth_header(settings.ado_pat)}"]
        if settings.ca_bundle:
            cfg.append(f"http.sslCAInfo={settings.ca_bundle}")
        elif not settings.verify_ssl:
            cfg.append("http.sslVerify=false")
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            _git(["init", "--bare", "-q"], cwd=path)
        b = repo.default_branch
        # Fetch only the default branch, with full history (phase 2 needs commits).
        _git(["fetch", "--prune", "--no-tags", repo.remote_url, f"+refs/heads/{b}:refs/heads/{b}"],
             cwd=path, extra_config=cfg)
        (path / cls.META).write_text(json.dumps({"name": repo.name, "default_branch": b,
                                                 "remote_url": repo.remote_url, "id": repo.id}))
        return cls(path, repo.name, b)

    @classmethod
    def open_all(cls, mirrors_dir: Path) -> list["GitMirror"]:
        out = []
        if not mirrors_dir.exists():
            return out
        for meta in sorted(mirrors_dir.glob(f"*/{cls.META}")):
            m = json.loads(meta.read_text())
            try:
                out.append(cls(meta.parent, m["name"], m["default_branch"]))
            except RuntimeError as e:
                log.warning("skipping mirror %s: %s", m["name"], e)
        return out

    def files(self) -> list[SourceFile]:
        raw = _git(["ls-tree", "-r", "-l", "-z", self.head_sha], cwd=self.path)
        out = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            _mode, typ, sha, size = meta.split()
            if typ != b"blob" or size == b"-" or int(size) > MAX_FILE_BYTES:
                continue
            p = path.decode("utf-8", errors="replace")
            kind = file_kind(p)
            if kind:
                out.append(SourceFile(p, kind, sha.decode()))
        return out

    def read_many(self, files: list[SourceFile]) -> Iterator[tuple[SourceFile, bytes]]:
        if not files:
            return
        proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=self.path,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            for f in files:
                proc.stdin.write(f.blob_id.encode() + b"\n")
                proc.stdin.flush()
                header = proc.stdout.readline().split()
                if len(header) < 3:  # "<sha> missing"
                    continue
                data = proc.stdout.read(int(header[2]))
                proc.stdout.read(1)  # trailing newline
                yield f, data
        finally:
            proc.stdin.close()
            proc.wait()


# ---------------------------------------------------------------- local folders (dev / tests)

class LocalRepo:
    """A plain directory treated as a repo snapshot. Each subfolder of --local-path is one repo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        self.default_branch = "local"
        self._files: list[SourceFile] | None = None
        self.head_sha = hashlib.sha1("".join(f.blob_id for f in self.files()).encode()).hexdigest()

    @classmethod
    def open_all(cls, root: Path) -> list["LocalRepo"]:
        return [cls(p) for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]

    def files(self) -> list[SourceFile]:
        if self._files is None:
            out = []
            for dirpath, dirnames, filenames in os.walk(self.path):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    full = Path(dirpath) / fn
                    rel = full.relative_to(self.path).as_posix()
                    kind = file_kind(rel)
                    if kind and full.stat().st_size <= MAX_FILE_BYTES:
                        out.append(SourceFile(rel, kind, hashlib.sha1(full.read_bytes()).hexdigest()))
            self._files = sorted(out, key=lambda f: f.path)
        return self._files

    def read_many(self, files: list[SourceFile]) -> Iterator[tuple[SourceFile, bytes]]:
        for f in files:
            yield f, (self.path / f.path).read_bytes()
