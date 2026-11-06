"""Turn parsed repos into the knowledge graph: create nodes, resolve references globally, add edges."""
from __future__ import annotations

import hashlib
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field

from kg.graph import Graph, tokens
from kg.parsers.java import base_type


@dataclass
class ParsedFile:
    path: str
    kind: str
    data: dict


@dataclass
class ParsedRepo:
    name: str
    default_branch: str
    head_sha: str
    files: list[ParsedFile] = field(default_factory=list)


def java_class_id(repo: str, fqn: str) -> str:
    return f"java:{repo}:{fqn}"


def java_method_ids(class_id: str, methods: list[dict]) -> list[str]:
    """Node ids for a class's methods, in order (overloads with equal erased signatures get ~2, ~3...)."""
    seen: dict[str, int] = defaultdict(int)
    out = []
    for m in methods:
        sig = ",".join(base_type(p["type"]) for p in m["params"])
        mid = f"{class_id}#{m['name']}({sig})"
        seen[mid] += 1
        out.append(mid + (f"~{seen[mid]}" if seen[mid] > 1 else ""))
    return out


def ts_symbol_id(repo: str, path: str, name: str) -> str:
    return f"ts:{repo}:{path}#{name}"


def file_id(repo: str, path: str) -> str:
    return f"file:{repo}:{path}"


def symbol_spans(repo: str, path: str, kind: str, data: dict) -> list[tuple[str, int, int, bool]]:
    """(node id, start_line, end_line, is_member) for the code symbols of one parsed file.
    is_member is True for methods/functions, False for containers (classes)."""
    out: list[tuple[str, int, int, bool]] = []
    if kind == "java":
        for t in data.get("types", []):
            cid = java_class_id(repo, t["fqn"])
            out.append((cid, t["start_line"], t["end_line"], False))
            for m, mid in zip(t["methods"], java_method_ids(cid, t["methods"])):
                out.append((mid, m["start_line"], m["end_line"], True))
    elif kind in ("ts", "tsx"):
        for s in data.get("symbols", []):
            out.append((ts_symbol_id(repo, path, s["name"]), s["start_line"], s["end_line"], s["kind"] != "class"))
    return out


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _is_test(path: str) -> bool:
    p = "/" + path.lower()
    return "/src/test/" in p or "/__tests__/" in p or ".test." in p or ".spec." in p or p.endswith("test.java")


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


# ------------------------------------------------------------------ HTTP path normalization

_VAR = re.compile(r"\{[^}]*\}")


def norm_path(raw: str) -> tuple[str, str | None, bool]:
    """-> (normalized lowercase path with '{}' params, host hint, has dynamic base)."""
    p = raw.strip()
    host = None
    m = re.match(r"^[a-zA-Z][\w+.-]*://([^/]+)", p)
    if m:
        host = m.group(1).split(":")[0].split("@")[-1]
        p = p[m.end():]
    p = re.split(r"[?#]", p, 1)[0]
    p = _VAR.sub("{}", p)
    segs = []
    for s in p.split("/"):
        if not s:
            continue
        if s.startswith(":") or s == "*" or "{}" in s:
            s = "{}"
        segs.append(s.lower())
    dynamic = bool(segs) and segs[0] == "{}" and not raw.strip().startswith("/")
    while segs and segs[0] == "{}" and dynamic:
        segs.pop(0)
    return "/" + "/".join(segs), host, dynamic


def _segs(p: str) -> list[str]:
    return [s for s in p.split("/") if s]


def _seg_match(a: list[str], b: list[str]) -> bool:
    literal = False
    for x, y in zip(a, b):
        if x == y:
            literal = literal or x != "{}"
        elif x != "{}" and y != "{}":
            return False
    return literal


def _specificity(a: list[str], b: list[str]) -> int:
    """Higher = closer structural match: equal literals 2, param-vs-param 1, param-vs-literal 0."""
    return sum(2 if x == y and x != "{}" else 1 if x == y else 0 for x, y in zip(a, b))


# ------------------------------------------------------------------ Spring constants

MAPPINGS = {"RequestMapping": None, "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
            "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}
STEREOTYPES = ["RestController", "Controller", "Service", "Repository", "Component", "Configuration",
               "ControllerAdvice", "RestControllerAdvice", "SpringBootApplication"]
INJECT_ANN = {"Autowired", "Inject", "Resource"}
CTOR_LOMBOK = {"RequiredArgsConstructor", "AllArgsConstructor"}
SPRING_DATA = {"JpaRepository", "CrudRepository", "PagingAndSortingRepository", "MongoRepository",
               "ReactiveCrudRepository", "ListCrudRepository", "Repository", "R2dbcRepository",
               "ElasticsearchRepository", "ListPagingAndSortingRepository"}
REST_TEMPLATE = {"getForObject": "GET", "getForEntity": "GET", "postForObject": "POST",
                 "postForEntity": "POST", "postForLocation": "POST", "patchForObject": "PATCH",
                 "put": "PUT", "delete": "DELETE", "exchange": None}
COMMON_METHOD_NAMES = {"get", "set", "of", "build", "builder", "apply", "accept", "test", "run", "call",
                       "equals", "hashCode", "toString", "valueOf", "values", "size", "isEmpty", "add",
                       "put", "remove", "contains", "map", "filter", "collect", "stream", "forEach",
                       "orElse", "orElseThrow", "close", "init", "execute", "handle", "process", "save",
                       "find", "findAll", "findById", "delete", "update", "create", "getId", "setId",
                       "getName", "setName", "length", "append", "format", "parse", "log", "info",
                       "debug", "error", "warn", "send", "start", "stop", "clear", "next", "hasNext"}
_PLACEHOLDER = re.compile(r"\$\{([^}:]+)(?::[^}]*)?\}")


def _ann(anns: list[dict], name: str) -> dict | None:
    return next((a for a in anns if a["name"] == name), None)


def _ann_paths(a: dict) -> list[str]:
    vals = a["args"].get("value") or a["args"].get("path") or []
    return vals or [""]


def _join_paths(*parts: str) -> str:
    out = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return "/" + out


# ------------------------------------------------------------------ Java model used during resolution

@dataclass
class JClass:
    id: str
    repo: str
    path: str
    package: str
    data: dict
    file: dict  # parsed file (imports etc.)
    outer: "JClass | None" = None
    supers: list["JClass"] = field(default_factory=list)
    methods: list[tuple[str, int, bool, str, dict]] = field(default_factory=list)  # name, argc, varargs, id, data

    @property
    def fqn(self) -> str:
        return self.data["fqn"]


class Builder:
    def __init__(self, repos: list[ParsedRepo]) -> None:
        self.repos = repos
        self.g = Graph()
        self.classes_by_fqn: dict[str, list[JClass]] = defaultdict(list)
        self.jclasses: list[JClass] = []
        self.methods_by_name: dict[str, list[str]] = defaultdict(list)
        self.method_owner: dict[str, JClass] = {}
        self.service_names: dict[str, set[str]] = defaultdict(set)  # repo -> spring app names + repo name
        self.config_keys: dict[str, set[str]] = defaultdict(set)  # repo -> keys
        self.endpoints: list[dict] = []
        self._endpoint_ids: set[str] = set()
        self.overridden_by: dict[str, list[str]] = defaultdict(list)
        self.http_clients: list[dict] = []  # api calls / feign methods to match against endpoints
        self.artifact_repo: dict[str, set[str]] = defaultdict(set)  # mvn id -> repos publishing it
        self.repo_deps_artifacts: dict[str, set[str]] = defaultdict(set)
        self.module_dirs: dict[str, list[tuple[str, str]]] = defaultdict(list)  # repo -> (dir, module id)

    # ------------------------------------------------------------ entry point
    def build(self) -> Graph:
        for r in self.repos:
            self.service_names[r.name].add(r.name.lower())
            self.g.node(f"repo:{r.name}", "Repository", name=r.name, repo=r.name, default_branch=r.default_branch,
                        head_sha=r.head_sha, tokens=tokens(r.name))
            for f in r.files:
                if f.kind in ("pom", "package_json", "spring_config"):
                    self._manifest(r, f)
        for r in self.repos:
            self._files(r)
        for r in self.repos:
            for f in r.files:
                if f.kind == "java":
                    self._java_index(r, f)
        self._java_link_types()
        for c in self.jclasses:
            self._java_class(c)
        self._java_overrides()
        for c in self.jclasses:
            self._java_calls(c)
        self._expose_interface_endpoints()
        for r in self.repos:
            self._ts_repo(r)
        self._match_http()
        self._repo_deps()
        self.g.finalize()
        return self.g

    # ------------------------------------------------------------ manifests & files
    def _manifest(self, r: ParsedRepo, f: ParsedFile) -> None:
        d = f.data
        dir_ = posixpath.dirname(f.path)
        if f.kind == "spring_config":
            if d.get("application_name"):
                self.service_names[r.name].add(d["application_name"].lower())
            fid = f"file:{r.name}:{f.path}"
            for k in d["keys"]:
                self.config_keys[r.name].add(k)
                cid = f"cfg:{r.name}:{k}"
                self.g.node(cid, "ConfigProperty", name=k, key=k, repo=r.name, defined=True, tokens=tokens(k))
                self.g.edge("DEFINES", fid, cid, profiles=d.get("profiles") or None)
            return
        mid = f"module:{r.name}:{dir_ or '.'}"
        self.module_dirs[r.name].append((dir_, mid))
        if f.kind == "pom":
            art = f"mvn:{d['group']}:{d['artifact']}"
            self.g.node(mid, "Module", name=d["artifact"], repo=r.name, path=dir_ or ".", build="maven",
                        group=d["group"], artifact=d["artifact"], version=d["version"], packaging=d["packaging"],
                        tokens=tokens(d["artifact"]))
            self.g.node(art, "MavenArtifact", name=d["artifact"], group=d["group"], artifact=d["artifact"])
            self.g.edge("PUBLISHES_ARTIFACT", mid, art)
            self.artifact_repo[art].add(r.name)
            if d.get("parent"):
                p = d["parent"]
                pid = f"mvn:{p['group']}:{p['artifact']}"
                self.g.node(pid, "MavenArtifact", name=p["artifact"], group=p["group"], artifact=p["artifact"])
                self.g.edge("DEPENDS_ON", mid, pid, scope="parent", version=p.get("version"))
                self.repo_deps_artifacts[r.name].add(pid)
            for dep in d["dependencies"]:
                if not dep["group"] or not dep["artifact"]:
                    continue
                did = f"mvn:{dep['group']}:{dep['artifact']}"
                self.g.node(did, "MavenArtifact", name=dep["artifact"], group=dep["group"], artifact=dep["artifact"])
                self.g.edge("DEPENDS_ON", mid, did, scope=dep["scope"], version=dep.get("version"))
                self.repo_deps_artifacts[r.name].add(did)
        else:
            name = d.get("name") or posixpath.basename(dir_) or r.name
            self.g.node(mid, "Module", name=name, repo=r.name, path=dir_ or ".", build="npm", tokens=tokens(name))
            for dep in d["dependencies"]:
                nid = f"npm:{dep['name']}"
                self.g.node(nid, "NpmPackage", name=dep["name"])
                self.g.edge("DEPENDS_ON", mid, nid, scope=dep["scope"], version=dep.get("version"))
        self.g.edge("CONTAINS", f"repo:{r.name}", mid)

    def _module_of(self, repo: str, path: str) -> str | None:
        best, best_len = None, -1
        for d, mid in self.module_dirs[repo]:
            if (d == "" or path.startswith(d + "/")) and len(d) > best_len:
                best, best_len = mid, len(d)
        return best

    def _files(self, r: ParsedRepo) -> None:
        for f in r.files:
            if f.kind == "tsconfig":
                continue
            fid = f"file:{r.name}:{f.path}"
            lang = {"java": "java", "ts": "typescript", "tsx": "typescript"}.get(f.kind, f.kind)
            test = _is_test(f.path)
            self.g.node(fid, "File", {"Test"} if test else None, name=posixpath.basename(f.path), path=f.path,
                        repo=r.name, lang=lang, lines=f.data.get("lines"), is_test=test,
                        parse_errors=f.data.get("has_errors") or None, tokens=tokens(f.path))
            self.g.edge("CONTAINS", f"repo:{r.name}", fid)
            mid = self._module_of(r.name, f.path)
            if mid:
                self.g.edge("CONTAINS", mid, fid)

    # ------------------------------------------------------------ Java: index & type resolution
    def _java_index(self, r: ParsedRepo, f: ParsedFile) -> None:
        by_qual: dict[str, JClass] = {}
        for t in f.data["types"]:
            c = JClass(f"java:{r.name}:{t['fqn']}", r.name, f.path, f.data["package"], t, f.data)
            outer_qual = t["qual"].rpartition(".")[0]
            c.outer = by_qual.get(outer_qual) if outer_qual else None
            by_qual[t["qual"]] = c
            self.classes_by_fqn[t["fqn"]].append(c)
            self.jclasses.append(c)
            for m, mid in zip(t["methods"], java_method_ids(c.id, t["methods"])):
                varargs = any(p.get("varargs") for p in m["params"])
                c.methods.append((m["name"], len(m["params"]), varargs, mid, m))
                self.methods_by_name[m["name"]].append(mid)
                self.method_owner[mid] = c

    def _pick(self, cands: list[JClass], repo: str) -> list[JClass]:
        same = [c for c in cands if c.repo == repo]
        return same or cands

    def resolve_type(self, raw: str, ctx: JClass) -> tuple[str | None, list[JClass]]:
        """Resolve a type as written inside `ctx` -> (fqn or None for primitives/unknown, internal classes)."""
        b = base_type(raw)
        if not b or b in ("void", "var") or b[0].islower() and "." not in b:
            return None, []
        file = ctx.file
        if "." in b:
            if b in self.classes_by_fqn:
                return b, self._pick(self.classes_by_fqn[b], ctx.repo)
            head, _, rest = b.partition(".")
            hf, hc = self.resolve_type(head, ctx)
            if hf:
                full = f"{hf}.{rest}"
                return full, self._pick(self.classes_by_fqn.get(full, []), ctx.repo)
            return b, []
        # nested types visible from ctx (own members, then outer classes' members)
        o: JClass | None = ctx
        while o is not None:
            cand = f"{o.fqn}.{b}"
            if cand in self.classes_by_fqn:
                return cand, self._pick(self.classes_by_fqn[cand], ctx.repo)
            o = o.outer
        # inherited nested types are rare; skipped
        for imp in file["imports"]:
            if imp.rpartition(".")[2] == b:
                return imp, self._pick(self.classes_by_fqn.get(imp, []), ctx.repo)
        pkg = file["package"]
        cand = f"{pkg}.{b}" if pkg else b
        if cand in self.classes_by_fqn:
            return cand, self._pick(self.classes_by_fqn[cand], ctx.repo)
        for w in file["wildcard_imports"]:
            cand = f"{w}.{b}"
            if cand in self.classes_by_fqn:
                return cand, self._pick(self.classes_by_fqn[cand], ctx.repo)
        return (None if b in _JAVA_LANG else b), []

    def _java_link_types(self) -> None:
        for c in self.jclasses:
            for raw in c.data["extends"] + c.data["implements"]:
                _fqn, cls = self.resolve_type(raw, c)
                c.supers.extend(x for x in cls if x is not c)

    # ------------------------------------------------------------ Java: nodes & declarative edges
    def _java_class(self, c: JClass) -> None:
        t, repo, g = c.data, c.repo, self.g
        anns = t["annotations"]
        ann_names = [a["name"] for a in anns]
        labels = set()
        kind_label = {"interface": "Interface", "enum": "Enum", "record": "Record", "annotation": "Annotation"}
        if t["kind"] in kind_label:
            labels.add(kind_label[t["kind"]])
        stereotype = next((s for s in STEREOTYPES if s in ann_names), None)
        if stereotype in ("RestController", "Controller"):
            labels.add("Controller")
        if _is_test(c.path):
            labels.add("Test")
        feign = _ann(anns, "FeignClient")
        if feign:
            labels.add("FeignClient")
        entity = _ann(anns, "Entity") or _ann(anns, "Document")
        if entity:
            labels.add("JpaEntity")
        code_hash = _sha(str(t["start_line"]) + "".join(m[4]["code"] for m in c.methods))
        g.node(c.id, "Class", labels, name=t["name"], fqn=t["fqn"], kind=t["kind"], repo=repo, path=c.path,
               lang="java", start_line=t["start_line"], end_line=t["end_line"], doc=t.get("doc"),
               annotations=[a["text"] for a in anns], modifiers=t["modifiers"], stereotype=stereotype,
               enum_constants=t.get("enum_constants"), is_test=_is_test(c.path), hash=code_hash,
               tokens=tokens(t["name"], c.package, t.get("doc")))
        fid = f"file:{repo}:{c.path}"
        if c.outer is not None:
            g.edge("CONTAINS", c.outer.id, c.id)
        else:
            g.edge("CONTAINS", fid, c.id)
            if c.package:
                pid = f"pkg:{repo}:{c.package}"
                g.node(pid, "Package", name=c.package, repo=repo, tokens=tokens(c.package))
                g.edge("CONTAINS", f"repo:{repo}", pid)
                g.edge("CONTAINS", pid, c.id)

        # supertypes
        for rel, raws in (("EXTENDS", t["extends"]), ("IMPLEMENTS", t["implements"])):
            if t["kind"] == "interface":
                rel = "EXTENDS"
            for raw in raws:
                fqn, cls = self.resolve_type(raw, c)
                for x in cls:
                    g.edge(rel, c.id, x.id)
                if not cls and fqn:
                    eid = f"javaext:{fqn}"
                    g.node(eid, "ExternalType", name=fqn.rpartition(".")[2], fqn=fqn)
                    g.edge(rel, c.id, eid)
                # Spring Data repository -> entity
                if base_type(raw).rpartition(".")[2] in SPRING_DATA and "<" in raw:
                    g.nodes[c.id].extra_labels.add("SpringRepository")
                    first_arg = raw[raw.index("<") + 1:].split(",")[0].strip()
                    _f, ents = self.resolve_type(first_arg, c)
                    for e in ents:
                        g.edge("REPOSITORY_OF", c.id, e.id)

        # JPA table
        if entity:
            tname = None
            tab = _ann(anns, "Table")
            if tab and (tab["args"].get("name") or tab["args"].get("value")):
                tname = (tab["args"].get("name") or tab["args"].get("value"))[0]
            if entity["name"] == "Document" and (entity["args"].get("collection") or entity["args"].get("value")):
                tname = (entity["args"].get("collection") or entity["args"].get("value"))[0]
            inferred = tname is None
            tname = (tname or _snake(t["name"])).lower()
            tid = f"table:{tname}"
            g.node(tid, "Table", name=tname, kind="collection" if entity["name"] == "Document" else "table")
            g.edge("MAPS_TO", c.id, tid, inferred=inferred or None)

        # config binding
        cp = _ann(anns, "ConfigurationProperties")
        if cp:
            prefix = (cp["args"].get("prefix") or cp["args"].get("value") or [""])[0]
            if prefix:
                g.nodes[c.id].props["config_prefix"] = prefix
                for k in self.config_keys[repo]:
                    if k.startswith(prefix + "."):
                        g.edge("READS_CONFIG", c.id, f"cfg:{repo}:{k}", via="ConfigurationProperties")

        # fields
        lombok_ctor = any(n in CTOR_LOMBOK for n in ann_names)
        ctor_param_types = {base_type(p["type"]) for m in t["methods"] if m["is_constructor"] for p in m["params"]}
        for f in t["fields"]:
            fid_ = f"{c.id}#{f['name']}"
            g.node(fid_, "Field", name=f["name"], type=f["type"], repo=repo, path=c.path, lang="java",
                   start_line=f["line"], annotations=[a["text"] for a in f["annotations"]],
                   modifiers=f["modifiers"], tokens=tokens(f["name"]))
            g.edge("CONTAINS", c.id, fid_)
            self._value_refs(fid_, repo, f["annotations"])
            f_ann = {a["name"] for a in f["annotations"]}
            injected = bool(f_ann & INJECT_ANN) or (
                stereotype and "static" not in f["modifiers"] and
                (("final" in f["modifiers"] and lombok_ctor) or base_type(f["type"]) in ctor_param_types))
            if injected:
                _fq, cls = self.resolve_type(f["type"], c)
                for x in cls:
                    g.edge("INJECTS", c.id, x.id, via=f["name"])

        # class-level mapping prefix (controllers and Feign clients)
        class_map = _ann(anns, "RequestMapping")
        prefixes = _ann_paths(class_map) if class_map else [""]
        feign_path = ""
        if feign:
            feign_path = (feign["args"].get("path") or [""])[0]
            fname = (feign["args"].get("name") or feign["args"].get("value") or [""])[0]
            furl = (feign["args"].get("url") or [""])[0]
            g.nodes[c.id].props.update({k: v for k, v in (("feign_name", fname), ("feign_url", furl)) if v})

        for name, argc, _va, mid, m in c.methods:
            params = ", ".join(f"{p['type']} {p['name']}" for p in m["params"])
            sig = f"{m['return_type'] + ' ' if m['return_type'] else ''}{t['name'] if m['is_constructor'] else name}({params})"
            g.node(mid, "Method", {"Test"} if _is_test(c.path) else None, name=name, signature=sig,
                   return_type=m["return_type"] or None, params=[f"{p['type']} {p['name']}" for p in m["params"]],
                   annotations=[a["text"] for a in m["annotations"]], modifiers=m["modifiers"],
                   is_constructor=m["is_constructor"], repo=repo, path=c.path, lang="java",
                   start_line=m["start_line"], end_line=m["end_line"], doc=m.get("doc"), code=m["code"],
                   hash=_sha(m["code"]), class_fqn=t["fqn"], tokens=tokens(name, t["name"], m.get("doc")))
            g.edge("CONTAINS", c.id, mid)
            self._value_refs(mid, repo, m["annotations"])
            for p in m["params"]:
                self._value_refs(mid, repo, p.get("annotations") or [])
            kl = _ann(m["annotations"], "KafkaListener")
            if kl:
                for topic in kl["args"].get("topics") or kl["args"].get("value") or []:
                    g.node(f"topic:{topic}", "Topic", name=topic)
                    g.edge("SUBSCRIBES", mid, f"topic:{topic}")
            mapping = next((a for a in m["annotations"] if a["name"] in MAPPINGS), None)
            if not mapping:
                continue
            verb = MAPPINGS[mapping["name"]]
            if verb is None:
                rm = mapping["args"].get("method") or []
                verbs = [v.rpartition(".")[2].upper() for v in rm] or ["ANY"]
            else:
                verbs = [verb]
            paths = [_join_paths(feign_path, pre, mp) for pre in prefixes for mp in _ann_paths(mapping)]
            if feign:
                g.nodes[mid].props.update(http_method=verbs[0], http_path=paths[0])
                self.http_clients.append({"src": mid, "repo": repo, "method": verbs[0], "url": paths[0],
                                          "service": (feign["args"].get("name") or feign["args"].get("value") or [None])[0],
                                          "kind": "feign"})
                continue
            for v in verbs:
                for p in paths:
                    self._endpoint(repo, v, p, mid, c, m)

    def _endpoint(self, repo: str, verb: str, path: str, mid: str, c: JClass, m: dict) -> None:
        eid = f"ep:{repo}:{verb}:{path}"
        norm, _h, _d = norm_path(path)
        self.g.node(eid, "Endpoint", name=f"{verb} {path}", http_method=verb, path=path, path_norm=norm, repo=repo,
                    file=c.path, start_line=m["start_line"], handler=mid,
                    service_names=sorted(self.service_names[repo]), tokens=tokens(path.replace("/", " ")))
        self.g.edge("EXPOSES", mid, eid)
        if eid not in self._endpoint_ids:
            self._endpoint_ids.add(eid)
            self.endpoints.append({"id": eid, "repo": repo, "method": verb, "segs": _segs(norm), "decl": mid})

    def _value_refs(self, src: str, repo: str, anns: list[dict]) -> None:
        a = _ann(anns, "Value")
        if not a:
            return
        for v in a["args"].get("value", []):
            for key in _PLACEHOLDER.findall(v):
                cid = f"cfg:{repo}:{key}"
                if cid not in self.g.nodes:
                    self.g.node(cid, "ConfigProperty", name=key, key=key, repo=repo, defined=False, tokens=tokens(key))
                self.g.edge("READS_CONFIG", src, cid, via="Value")

    # ------------------------------------------------------------ Java: overrides & calls
    def _ancestors(self, c: JClass) -> list[JClass]:
        out, seen, queue = [], {c.id}, list(c.supers)
        while queue:
            x = queue.pop(0)
            if x.id in seen:
                continue
            seen.add(x.id)
            out.append(x)
            queue.extend(x.supers)
        return out

    def _java_overrides(self) -> None:
        for c in self.jclasses:
            anc = self._ancestors(c)
            for name, argc, _va, mid, m in c.methods:
                if m["is_constructor"] or "static" in m["modifiers"] or "private" in m["modifiers"]:
                    continue
                for a in anc:
                    hit = [x for x in a.methods if x[0] == name and x[1] == argc]
                    for h in hit:
                        self.g.edge("OVERRIDES", mid, h[3])
                        self.overridden_by[h[3]].append(mid)
                    if hit:
                        break

    def _find_methods(self, cls: JClass, name: str, argc: int, include_outer: bool = False) -> tuple[list[str], bool]:
        """Search cls, its ancestors (and optionally outer classes) -> (method ids, exact argc match)."""
        chain = [cls] + self._ancestors(cls)
        o = cls.outer
        while include_outer and o is not None:
            chain += [o] + self._ancestors(o)
            o = o.outer
        loose: list[str] = []
        for k in chain:
            exact = [x[3] for x in k.methods if x[0] == name and (x[1] == argc or (x[2] and argc >= x[1] - 1))]
            if exact:
                return exact, True
            if not loose:
                loose = [x[3] for x in k.methods if x[0] == name]
        return loose, False

    def _field_type(self, cls: JClass, field_name: str) -> tuple[str, JClass] | None:
        o: JClass | None = cls
        while o is not None:
            for k in [o] + self._ancestors(o):
                for f in k.data["fields"]:
                    if f["name"] == field_name:
                        return f["type"], k
                for rc in k.data.get("record_components", []):
                    if rc["name"] == field_name:
                        return rc["type"], k
            o = o.outer
        return None

    def _java_calls(self, c: JClass) -> None:
        g = self.g
        for _name, _argc, _va, mid, m in c.methods:
            for call in m["calls"]:
                targets, conf = self._resolve_call(c, call)
                for tid in targets:
                    g.edge("CALLS", mid, tid, confidence=conf, count=1, lines=[call["line"]])
                self._java_side_effects(c, mid, call)
            for created in m.get("creates", []):
                _f, cls = self.resolve_type(created, c)
                for x in cls:
                    g.edge("INSTANTIATES", mid, x.id, count=1)

    def _resolve_call(self, c: JClass, call: dict) -> tuple[list[str], float]:
        recv, name, argc = call.get("recv") or {}, call["name"], call["argc"]
        classes: list[JClass] = []
        if recv.get("this"):
            ids, exact = self._find_methods(c, name, argc, include_outer=True)
            return ids, 0.95 if exact else 0.8
        if recv.get("super"):
            for s in c.supers:
                ids, exact = self._find_methods(s, name, argc)
                if ids:
                    return ids, 0.95 if exact else 0.8
            return [], 0
        raw_type, ctx = None, c
        if "type" in recv:
            raw_type = recv["type"]
        elif "field" in recv:
            ft = self._field_type(c, recv["field"])
            if ft:
                raw_type, ctx = ft
        if raw_type is not None:
            fqn, classes = self.resolve_type(raw_type, ctx)
            if not classes:
                return [], 0  # external type (JDK, Spring, libraries): not in the graph
            out, exact_any = [], False
            for k in classes:
                ids, exact = self._find_methods(k, name, argc)
                out += ids
                exact_any = exact_any or exact
            conf = 0.95 if exact_any else 0.8
            if len(classes) > 1:
                conf *= 0.6
            return out, conf
        # unknown receiver (chained call etc.): unique-name fallback
        if name not in COMMON_METHOD_NAMES and len(name) > 3:
            cands = self.methods_by_name.get(name, [])
            if len(cands) == 1:
                return cands, 0.4
        return [], 0

    def _java_side_effects(self, c: JClass, mid: str, call: dict) -> None:
        recv = call.get("recv") or {}
        recv_text = recv.get("type") or recv.get("field") or recv.get("var") or ""
        if "field" in recv:
            ft = self._field_type(c, recv["field"])
            recv_text = (ft[0] if ft else "") + " " + recv["field"]
        name, arg0 = call["name"], call.get("arg0")
        # Kafka publish
        if name == "send" and arg0 and "kafkatemplate" in recv_text.lower():
            self.g.node(f"topic:{arg0}", "Topic", name=arg0)
            self.g.edge("PUBLISHES", mid, f"topic:{arg0}")
            return
        if not arg0 or not ("/" in arg0 or arg0.startswith("http")):
            return
        method = None
        if name in REST_TEMPLATE:
            if name in ("put", "delete", "exchange") and "rest" not in recv_text.lower():
                return
            method = REST_TEMPLATE[name] or (call.get("http_method") or "ANY").upper()
            client = "RestTemplate"
        elif name == "uri":
            chain = (recv.get("chain") or "").upper()
            method = chain if chain in ("GET", "POST", "PUT", "DELETE", "PATCH") else "ANY"
            client = "WebClient"
        else:
            return
        aid = f"api:{c.repo}:{c.path}:{call['line']}"
        norm, host, _dyn = norm_path(arg0)
        self.g.node(aid, "ApiCall", name=f"{method} {arg0}", http_method=method, url=arg0, url_norm=norm,
                    client=client, repo=c.repo, path=c.path, start_line=call["line"], lang="java",
                    tokens=tokens(arg0.replace("/", " ")))
        self.g.edge("CALLS_API", mid, aid)
        self.http_clients.append({"src": aid, "repo": c.repo, "method": method, "url": arg0, "service": host,
                                  "kind": "rest"})

    def _expose_interface_endpoints(self) -> None:
        """API-first style: mapping annotations on an interface, implemented by a controller."""
        for e in list(self.endpoints):
            for impl in self.overridden_by.get(e["decl"], []):
                self.g.edge("EXPOSES", impl, e["id"], via="interface")

    # ------------------------------------------------------------ TypeScript / React
    def _ts_repo(self, r: ParsedRepo) -> None:
        files = {f.path: f for f in r.files if f.kind in ("ts", "tsx")}
        if not files:
            return
        tsconfigs = {posixpath.dirname(f.path): f.data for f in r.files if f.kind == "tsconfig"}
        pkg_dirs = [posixpath.dirname(f.path) for f in r.files if f.kind == "package_json"]
        sym_ids: dict[str, dict[str, str]] = {}
        g = self.g
        for path, f in files.items():
            ids = {}
            for s in f.data["symbols"]:
                sid = f"ts:{r.name}:{path}#{s['name']}"
                labels = set()
                if s["is_component"]:
                    labels.add("Component")
                if s["is_hook"]:
                    labels.add("Hook")
                if _is_test(path):
                    labels.add("Test")
                g.node(sid, "Class" if s["kind"] == "class" else "Function", labels, name=s["name"], kind=s["kind"],
                       repo=r.name, path=path, lang="typescript", start_line=s["start_line"],
                       end_line=s["end_line"], code=s["code"], hash=_sha(s["code"]),
                       exported=s["name"] in f.data["exports"].values() or None, tokens=tokens(s["name"], path))
                g.edge("CONTAINS", f"file:{r.name}:{path}", sid)
                ids[s["name"]] = sid
            sym_ids[path] = ids

        def resolve_spec(from_path: str, spec: str) -> str | None:
            bases = []
            if spec.startswith("."):
                bases.append(posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec)))
            else:
                # closest tsconfig/jsconfig first
                for d in sorted(tsconfigs, key=len, reverse=True):
                    if d and not from_path.startswith(d + "/"):
                        continue
                    tc = tsconfigs[d]
                    base_url = posixpath.normpath(posixpath.join(d, tc.get("baseUrl") or "."))
                    for pat, targets in (tc.get("paths") or {}).items():
                        pre = pat.rstrip("*")
                        if (pat.endswith("*") and spec.startswith(pre)) or spec == pat:
                            rest = spec[len(pre):] if pat.endswith("*") else ""
                            for tgt in targets:
                                bases.append(posixpath.normpath(posixpath.join(base_url, tgt.rstrip("*") + rest)))
                    if tc.get("baseUrl"):
                        bases.append(posixpath.normpath(posixpath.join(base_url, spec)))
                for alias in ("@/", "~/"):
                    if spec.startswith(alias):
                        for d in pkg_dirs or [""]:
                            bases.append(posixpath.normpath(posixpath.join(d, "src", spec[2:])))
            for b in bases:
                for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
                    if b + ext in files:
                        return b + ext
            return None

        def get_export(path: str, name: str, depth: int = 0) -> str | None:
            if depth > 6 or path not in files:
                return None
            d = files[path].data
            local = d["exports"].get(name)
            if local:
                if local in sym_ids[path]:
                    return sym_ids[path][local]
                b = binding(path, local, depth + 1)
                if b:
                    return b
            for rx in d["reexports"]:
                target = resolve_spec(path, rx["source"])
                if not target:
                    continue
                if rx["all"] and name != "default":
                    hit = get_export(target, name, depth + 1)
                    if hit:
                        return hit
                for imported, exported in rx["names"]:
                    if exported == name:
                        return get_export(target, imported, depth + 1)
            return None

        def binding(path: str, local: str, depth: int = 0) -> str | None:
            if local in sym_ids.get(path, {}):
                return sym_ids[path][local]
            for imp in files[path].data["imports"]:
                if not imp["source"]:
                    continue
                if imp["default"] == local or any(a == local for _n, a in imp["named"]):
                    target = resolve_spec(path, imp["source"])
                    if not target:
                        return None
                    if imp["default"] == local:
                        return get_export(target, "default", depth)
                    nm = next(n for n, a in imp["named"] if a == local)
                    return get_export(target, nm, depth)
            return None

        def ns_member(path: str, dotted: str) -> str | None:
            head, _, member = dotted.partition(".")
            if not member or "." in member:
                return None
            for imp in files[path].data["imports"]:
                if imp["namespace"] == head and imp["source"]:
                    target = resolve_spec(path, imp["source"])
                    return get_export(target, member) if target else None
            return None

        for path, f in files.items():
            fid = f"file:{r.name}:{path}"
            d = f.data
            for imp in d["imports"] + d["reexports"]:
                spec = imp["source"]
                if not spec:
                    continue
                target = resolve_spec(path, spec)
                if target:
                    g.edge("IMPORTS", fid, f"file:{r.name}:{target}")
                elif not spec.startswith("."):
                    pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
                    if not spec.startswith(("@/", "~/")):
                        g.node(f"npm:{pkg}", "NpmPackage", name=pkg)
                        g.edge("IMPORTS", fid, f"npm:{pkg}")
            units = [(sym_ids[path][s["name"]], s) for s in d["symbols"]] + [(fid, d["module"])]
            for src, s in units:
                for call in s["calls"]:
                    nm = call["name"]
                    tid = ns_member(path, nm) if "." in nm else binding(path, nm)
                    if tid and tid != src:
                        rel = "USES_HOOK" if re.match(r"use[A-Z0-9]", nm.rpartition(".")[2]) else "CALLS"
                        g.edge(rel, src, tid, count=1, lines=[call["line"]], confidence=0.9)
                for comp in s["jsx"]:
                    tid = ns_member(path, comp) if "." in comp else binding(path, comp)
                    if tid and tid != src:
                        g.edge("RENDERS", src, tid, count=1)
                for a in s["api_calls"]:
                    aid = f"api:{r.name}:{path}:{a['line']}"
                    norm, host, _dyn = norm_path(a["url"])
                    g.node(aid, "ApiCall", name=f"{a['method']} {a['url']}", http_method=a["method"], url=a["url"],
                           url_norm=norm, client=a["client"], repo=r.name, path=path, start_line=a["line"],
                           lang="typescript", tokens=tokens(a["url"].replace("/", " ")))
                    g.edge("CALLS_API", src, aid)
                    self.http_clients.append({"src": aid, "repo": r.name, "method": a["method"], "url": a["url"],
                                              "service": host, "kind": "frontend"})
                for rt in s["routes"]:
                    rid = f"route:{r.name}:{rt['path']}"
                    g.node(rid, "Route", name=rt["path"], path=rt["path"], repo=r.name, file=path,
                           start_line=rt["line"], tokens=tokens(rt["path"].replace("/", " ")))
                    g.edge("DEFINES_ROUTE", src, rid)
                    if rt.get("component"):
                        comp = rt["component"]
                        tid = ns_member(path, comp) if "." in comp else binding(path, comp)
                        if tid:
                            g.edge("ROUTES_TO", rid, tid)

    # ------------------------------------------------------------ cross-repo HTTP matching
    def _match_http(self) -> None:
        by_len: dict[int, list[dict]] = defaultdict(list)
        for e in self.endpoints:
            by_len[len(e["segs"])].append(e)
        for hc in self.http_clients:
            norm, host, dynamic = norm_path(hc["url"])
            segs = _segs(norm)
            method = (hc["method"] or "ANY").upper()
            service = (hc.get("service") or "").lower() or None

            def ok_method(e: dict) -> bool:
                return method == "ANY" or e["method"] == "ANY" or e["method"] == method

            exact = [e for e in by_len.get(len(segs), []) if ok_method(e) and _seg_match(segs, e["segs"])]
            suffix: list[dict] = []
            if not exact:
                for n, eps in by_len.items():
                    if n < 2:
                        continue
                    for e in eps:
                        if not ok_method(e):
                            continue
                        if n < len(segs) and _seg_match(segs[-n:], e["segs"]):
                            suffix.append(e)  # gateway / context-path prefix on the caller side
                        elif dynamic and n > len(segs) >= 2 and _seg_match(segs, e["segs"][-len(segs):]):
                            suffix.append(e)  # caller uses a dynamic base URL
            cands, conf = (exact, 0.9) if exact else (suffix, 0.6)
            if not cands:
                continue
            # keep only the most specific matches: '/orders/{}' prefers '/orders/{id}' over '/orders/search'
            def spec(e: dict) -> int:
                n = min(len(segs), len(e["segs"]))
                return _specificity(segs[-n:], e["segs"][-n:])
            best = max(spec(e) for e in cands)
            cands = [e for e in cands if spec(e) == best]
            if service:
                svc = [e for e in cands if service in self.service_names[e["repo"]]]
                if svc:
                    cands, conf = svc, min(conf + 0.05, 0.95)
            # a client calling its own repo is only plausible for frontends bundled with a backend
            if len(cands) > 5:
                continue  # too ambiguous to be useful
            if len(cands) > 1:
                conf *= 0.7
            for e in cands:
                self.g.edge("CONSUMES", hc["src"], e["id"], confidence=round(conf, 2), kind=hc["kind"])

    def _repo_deps(self) -> None:
        for repo, arts in self.repo_deps_artifacts.items():
            for a in arts:
                for other in self.artifact_repo.get(a, ()):
                    if other != repo:
                        self.g.edge("DEPENDS_ON_REPO", f"repo:{repo}", f"repo:{other}", via=a)


_JAVA_LANG = {"String", "Object", "Integer", "Long", "Short", "Byte", "Double", "Float", "Boolean", "Character",
              "Number", "Math", "System", "Thread", "Runnable", "Exception", "RuntimeException", "Throwable",
              "Error", "Iterable", "Comparable", "CharSequence", "StringBuilder", "Class", "Enum", "Record",
              "Void", "Override", "Deprecated", "FunctionalInterface", "SuppressWarnings",
              "IllegalArgumentException", "IllegalStateException", "NullPointerException",
              "UnsupportedOperationException", "AutoCloseable", "Cloneable"}


def build_graph(repos: list[ParsedRepo]) -> Graph:
    return Builder(repos).build()
