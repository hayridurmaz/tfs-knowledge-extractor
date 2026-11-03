"""Java source -> declarations, annotations and call sites (syntax only; resolution happens later)."""
from __future__ import annotations

import re

import tree_sitter_java
from tree_sitter import Language, Node, Parser

_PARSER = Parser(Language(tree_sitter_java.language()))

TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
MAX_CODE = 20_000
_IMPORT = re.compile(r"import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;")


def _t(n: Node | None) -> str:
    return n.text.decode("utf-8", errors="replace") if n is not None else ""


def base_type(type_text: str) -> str:
    """'List<Foo>' -> 'List', 'Foo[]' -> 'Foo', 'final Foo' -> 'Foo', 'a.b.Foo<X>' -> 'a.b.Foo'."""
    t = re.sub(r"@\w+(\([^)]*\))?\s*", "", type_text)
    depth, out = 0, []
    for ch in t:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    words = "".join(out).replace("[]", "").replace("...", "").split()
    return words[-1] if words else type_text.strip()


def string_value(n: Node, consts: dict[str, str] | None = None) -> str | None:
    """Evaluate a string literal / concatenation; non-literal parts become '{}'."""
    if n.type == "string_literal":
        return _t(n)[1:-1]
    if n.type == "text_block":
        return _t(n)[3:-3].strip()
    if n.type == "binary_expression" and _t(n.child_by_field_name("operator")) == "+":
        left = string_value(n.child_by_field_name("left"), consts)
        right = string_value(n.child_by_field_name("right"), consts)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    if n.type == "parenthesized_expression" and n.named_child_count:
        return string_value(n.named_children[0], consts)
    if consts is not None and n.type in ("identifier", "field_access"):
        name = _t(n).split(".")[-1]
        if name in consts:
            return consts[name]
    return None


def _annotations(mods: Node | None, consts: dict[str, str] | None = None) -> list[dict]:
    out = []
    if mods is None:
        return out
    for a in mods.named_children:
        if a.type not in ("marker_annotation", "annotation"):
            continue
        name = _t(a.child_by_field_name("name")).split(".")[-1]
        args: dict[str, list[str]] = {}
        arglist = a.child_by_field_name("arguments")
        if arglist is not None:
            for arg in arglist.named_children:
                if arg.type == "element_value_pair":
                    key = _t(arg.child_by_field_name("key"))
                    val = arg.child_by_field_name("value")
                else:
                    key, val = "value", arg
                args[key] = _ann_values(val, consts)
        out.append({"name": name, "args": args, "text": _t(a)[:300]})
    return out


def _ann_values(v: Node | None, consts: dict[str, str] | None = None) -> list[str]:
    if v is None:
        return []
    if v.type == "element_value_array_initializer":
        vals = []
        for c in v.named_children:
            vals.extend(_ann_values(c, consts))
        return vals
    s = string_value(v, consts)
    return [s if s is not None else _t(v)]


def _modifiers(n: Node) -> Node | None:
    for c in n.children:
        if c.type == "modifiers":
            return c
    return None


def _modifier_words(mods: Node | None) -> list[str]:
    if mods is None:
        return []
    return [_t(c) for c in mods.children if c.type not in ("marker_annotation", "annotation") and _t(c).isalpha()]


def _doc(n: Node) -> str | None:
    p = n.prev_sibling
    if p is not None and p.type == "block_comment" and _t(p).startswith("/**"):
        text = re.sub(r"^\s*\*+", "", _t(p)[3:-2], flags=re.M)
        return " ".join(text.split())[:1000] or None
    return None


def _params(params: Node | None) -> list[dict]:
    out = []
    if params is None:
        return out
    for p in params.named_children:
        if p.type in ("formal_parameter", "spread_parameter"):
            name_node = p.child_by_field_name("name")
            type_node = p.child_by_field_name("type")
            if p.type == "spread_parameter":
                type_node = next((c for c in p.named_children if c.type not in ("modifiers", "variable_declarator")), None)
                decl = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                name_node = decl.child_by_field_name("name") if decl is not None else None
            out.append({"type": _t(type_node), "name": _t(name_node),
                        "annotations": _annotations(_modifiers(p)), "varargs": p.type == "spread_parameter"})
    return out


class _MethodScanner:
    """Collects call sites in a method body, inferring the receiver's static type where syntax allows."""

    def __init__(self, scope_types: dict[str, str], static_imports: dict[str, str], consts: dict[str, str]):
        self.scope = dict(scope_types)  # var name -> raw type text
        self.static_imports = static_imports
        self.consts = consts
        self.calls: list[dict] = []
        self.creates: set[str] = set()

    def scan(self, body: Node) -> None:
        stack = [body]
        while stack:
            n = stack.pop()
            t = n.type
            if t in TYPE_DECLS:  # local class: its members are not this method's calls
                continue
            if t == "local_variable_declaration":
                typ = _t(n.child_by_field_name("type"))
                for d in n.children_by_field_name("declarator"):
                    name = _t(d.child_by_field_name("name"))
                    if typ == "var":
                        val = d.child_by_field_name("value")
                        typ_eff = _t(val.child_by_field_name("type")) if val is not None and val.type == "object_creation_expression" else ""
                        if typ_eff:
                            self.scope[name] = typ_eff
                    else:
                        self.scope[name] = typ
            elif t == "enhanced_for_statement":
                self.scope[_t(n.child_by_field_name("name"))] = _t(n.child_by_field_name("type"))
            elif t == "catch_formal_parameter":
                tn = next((c for c in n.named_children if c.type == "catch_type"), None)
                nm = n.child_by_field_name("name")
                if tn is not None and nm is not None:
                    self.scope[_t(nm)] = _t(tn).split("|")[0].strip()
            elif t == "object_creation_expression":
                self.creates.add(base_type(_t(n.child_by_field_name("type"))))
            elif t == "method_invocation":
                self.calls.append(self._call(n))
            stack.extend(reversed(n.named_children))

    def _call(self, n: Node) -> dict:
        name = _t(n.child_by_field_name("name"))
        obj = n.child_by_field_name("object")
        args = n.child_by_field_name("arguments")
        arg_nodes = args.named_children if args is not None else []
        call = {"name": name, "argc": len(arg_nodes), "line": n.start_point[0] + 1}
        if obj is None:
            if name in self.static_imports:
                call["recv"] = {"type": self.static_imports[name], "static": True}
            else:
                call["recv"] = {"this": True}
        elif obj.type == "this":
            call["recv"] = {"this": True}
        elif obj.type == "super":
            call["recv"] = {"super": True}
        elif obj.type == "identifier":
            v = _t(obj)
            if v in self.scope:
                call["recv"] = {"type": self.scope[v], "var": v}
            elif v[:1].isupper():
                call["recv"] = {"type": v, "static": True}
            else:
                call["recv"] = {"field": v}  # resolved against class fields (incl. inherited) later
        elif obj.type == "field_access" and _t(obj.child_by_field_name("object")) == "this":
            call["recv"] = {"field": _t(obj.child_by_field_name("field"))}
        elif obj.type in ("field_access", "scoped_identifier") and _t(obj)[:1].islower() and re.fullmatch(r"[\w.]+", _t(obj)) and _t(obj).split(".")[-1][:1].isupper():
            call["recv"] = {"type": _t(obj), "static": True}  # fully qualified static call
        elif obj.type == "method_invocation":
            call["recv"] = {"chain": _t(obj.child_by_field_name("name"))}
        else:
            call["recv"] = {}
        if arg_nodes:
            s = string_value(arg_nodes[0], self.consts)
            if s is not None:
                call["arg0"] = s[:500]
            m = re.search(r"HttpMethod\.(\w+)", _t(args))
            if m:
                call["http_method"] = m.group(1)
        return call


def parse_java(src: bytes) -> dict:
    tree = _PARSER.parse(src)
    root = tree.root_node
    package = ""
    imports, wildcard_imports, static_imports = [], [], {}
    types: list[dict] = []
    for n in root.named_children:
        if n.type == "package_declaration":
            package = _t(next((c for c in n.named_children if c.type in ("scoped_identifier", "identifier")), None))
        elif n.type == "import_declaration":
            m = _IMPORT.match(_t(n))
            if not m:
                continue
            is_static, name, star = m.group(1), m.group(2), m.group(3)
            if is_static and not star:
                cls, _, member = name.rpartition(".")
                static_imports[member] = cls
            elif is_static and star:
                wildcard_imports.append(name)  # static wildcard: members of a class; treat class as importable
            elif star:
                wildcard_imports.append(name)
            else:
                imports.append(name)
        elif n.type in TYPE_DECLS:
            _type_decl(n, package, None, static_imports, types)
    return {"package": package, "imports": imports, "wildcard_imports": wildcard_imports,
            "static_imports": static_imports, "types": types, "lines": root.end_point[0] + 1,
            "has_errors": root.has_error}


def _type_decl(n: Node, package: str, outer: dict | None, static_imports: dict, out: list[dict]) -> None:
    kind = TYPE_DECLS[n.type]
    name = _t(n.child_by_field_name("name"))
    qual = f"{outer['qual']}.{name}" if outer else name
    mods = _modifiers(n)
    extends, implements = [], []
    sc = n.child_by_field_name("superclass")
    if sc is not None:
        extends = [_t(c) for c in sc.named_children]
    for c in n.children:
        if c.type == "super_interfaces":
            tl = next((x for x in c.named_children if x.type == "type_list"), None)
            implements = [_t(x) for x in (tl.named_children if tl is not None else [])]
        elif c.type == "extends_interfaces":
            tl = next((x for x in c.named_children if x.type == "type_list"), None)
            extends = [_t(x) for x in (tl.named_children if tl is not None else [])]
    body = n.child_by_field_name("body")
    t = {
        "name": name, "qual": qual, "fqn": f"{package}.{qual}" if package else qual, "kind": kind,
        "annotations": _annotations(mods), "modifiers": _modifier_words(mods),
        "extends": extends, "implements": implements, "doc": _doc(n),
        "start_line": n.start_point[0] + 1, "end_line": n.end_point[0] + 1,
        "fields": [], "methods": [], "consts": {}, "enum_constants": [],
    }
    if kind == "record":
        t["record_components"] = _params(n.child_by_field_name("parameters"))
    out.append(t)
    if body is None:
        return
    members = list(body.named_children)
    for m in list(members):
        if m.type == "enum_body_declarations":
            members.extend(m.named_children)
        elif m.type == "enum_constant":
            t["enum_constants"].append(_t(m.child_by_field_name("name")))
    # Fields first: method scanning needs field types and string constants.
    for m in members:
        if m.type in ("field_declaration", "constant_declaration"):
            fmods = _modifiers(m)
            typ = _t(m.child_by_field_name("type"))
            for d in m.children_by_field_name("declarator"):
                fname = _t(d.child_by_field_name("name"))
                val = d.child_by_field_name("value")
                f = {"name": fname, "type": typ, "annotations": _annotations(fmods),
                     "modifiers": _modifier_words(fmods), "line": m.start_point[0] + 1}
                if val is not None and typ == "String":
                    s = string_value(val, t["consts"])
                    if s is not None:
                        t["consts"][fname] = s
                t["fields"].append(f)
    if kind == "interface":
        for f in t["fields"]:
            f["modifiers"] = sorted(set(f["modifiers"]) | {"static", "final"})
    scope = {f["name"]: f["type"] for f in t["fields"]}
    for rc in t.get("record_components", []):
        scope[rc["name"]] = rc["type"]
    for m in members:
        if m.type in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
            t["methods"].append(_method(m, scope, static_imports, t["consts"]))
        elif m.type in TYPE_DECLS:
            _type_decl(m, package, t, static_imports, out)


def _method(m: Node, field_scope: dict, static_imports: dict, consts: dict) -> dict:
    mods = _modifiers(m)
    is_ctor = m.type != "method_declaration"
    params = _params(m.child_by_field_name("parameters"))
    scope = dict(field_scope)
    for p in params:
        scope[p["name"]] = p["type"]
    body = m.child_by_field_name("body")
    scanner = _MethodScanner(scope, static_imports, consts)
    if body is not None:
        scanner.scan(body)
    # Drop field types that a local/param shadowed? Kept: scanner already prefers latest assignment.
    code = _t(m)
    return {
        "name": "<init>" if is_ctor else _t(m.child_by_field_name("name")),
        "is_constructor": is_ctor,
        "return_type": "" if is_ctor else _t(m.child_by_field_name("type")),
        "params": params,
        "annotations": _annotations(mods, consts),  # resolves @GetMapping(BASE + "/{id}") with class constants
        "modifiers": _modifier_words(mods),
        "doc": _doc(m),
        "start_line": m.start_point[0] + 1,
        "end_line": m.end_point[0] + 1,
        "has_body": body is not None,
        "calls": scanner.calls,
        "creates": sorted(scanner.creates),
        "code": code[:MAX_CODE],
    }
