"""TypeScript / JavaScript / React source -> imports, exports, symbols, JSX usage, HTTP calls and routes."""
from __future__ import annotations

import re

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

_TS = Parser(Language(tree_sitter_typescript.language_typescript()))
_TSX = Parser(Language(tree_sitter_typescript.language_tsx()))

HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head"}
FUNC_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}
MAX_CODE = 20_000
_PASCAL = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
_HOOK = re.compile(r"^use[A-Z0-9]")


def _t(n: Node | None) -> str:
    return n.text.decode("utf-8", errors="replace") if n is not None else ""


def _str(n: Node | None) -> str | None:
    if n is None:
        return None
    if n.type == "string":
        return "".join(_t(c) for c in n.named_children if c.type in ("string_fragment", "escape_sequence"))
    if n.type == "template_string" and not any(c.type == "template_substitution" for c in n.named_children):
        return _t(n)[1:-1]
    return None


def url_value(n: Node | None, consts: dict[str, str]) -> str | None:
    """Evaluate a URL expression; unknown parts become '{}'. None when nothing literal is present."""
    if n is None:
        return None
    s = _str(n)
    if s is not None:
        return s
    if n.type == "template_string":
        parts = []
        for c in n.named_children:
            if c.type == "string_fragment":
                parts.append(_t(c))
            elif c.type == "template_substitution":
                inner = c.named_children[0] if c.named_children else None
                parts.append(consts.get(_t(inner), "{}") if inner is not None and inner.type == "identifier" else "{}")
        return "".join(parts)
    if n.type == "binary_expression" and _t(n.child_by_field_name("operator")) == "+":
        left = url_value(n.child_by_field_name("left"), consts)
        right = url_value(n.child_by_field_name("right"), consts)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    if n.type == "identifier" and _t(n) in consts:
        return consts[_t(n)]
    if n.type == "parenthesized_expression" and n.named_children:
        return url_value(n.named_children[0], consts)
    return None


def _urlish(u: str | None) -> bool:
    return bool(u) and ("/" in u or u.startswith("http"))


def _pairs(obj: Node | None) -> dict[str, Node]:
    out = {}
    if obj is not None and obj.type == "object":
        for p in obj.named_children:
            if p.type == "pair":
                key = p.child_by_field_name("key")
                k = _str(key) if key is not None and key.type == "string" else _t(key)
                out[k] = p.child_by_field_name("value")
    return out


def _jsx_name(n: Node) -> str:
    return _t(n.child_by_field_name("name"))


def _first_jsx_component(n: Node | None) -> str | None:
    if n is None:
        return None
    stack = [n]
    while stack:
        x = stack.pop()
        if x.type in ("jsx_self_closing_element", "jsx_opening_element"):
            name = _jsx_name(x)
            if name[:1].isupper():
                return name
        stack.extend(reversed(x.named_children))
    return None


def _join(prefix: str, path: str) -> str:
    if path.startswith("/") or not prefix:
        return path
    return prefix.rstrip("/") + "/" + path


class _Scanner:
    def __init__(self, consts: dict[str, str]):
        self.consts = consts
        self.calls: list[dict] = []
        self.jsx: set[str] = set()
        self.has_jsx = False
        self.api_calls: list[dict] = []
        self.routes: list[dict] = []

    def scan(self, root: Node, prefix: str = "") -> None:
        stack: list[tuple[Node, str]] = [(root, prefix)]
        while stack:
            n, pre = stack.pop()
            t = n.type
            child_prefix = pre
            if t in ("jsx_self_closing_element", "jsx_opening_element"):
                self.has_jsx = True
                name = _jsx_name(n)
                if name[:1].isupper():
                    self.jsx.add(name)
                if name.split(".")[-1].endswith("Route"):
                    r = self._jsx_route(n, pre)
                    if r:
                        self.routes.append(r)
            elif t == "jsx_element":
                opening = n.child_by_field_name("open_tag")
                if opening is not None and _jsx_name(opening).split(".")[-1].endswith("Route"):
                    path = self._attr_str(opening, "path")
                    if path is not None:
                        child_prefix = _join(pre, path)
                    # The opening element itself is processed with the outer prefix.
                    stack.append((opening, pre))
                    stack.extend((c, child_prefix) for c in reversed(n.named_children)
                                 if c.start_byte != opening.start_byte)
                    continue
            elif t == "object":
                pairs = _pairs(n)
                if "path" in pairs and _str(pairs["path"]) is not None:
                    path = _join(pre, _str(pairs["path"]))
                    comp = (_first_jsx_component(pairs.get("element"))
                            or self._ident(pairs.get("Component")) or self._ident(pairs.get("component")))
                    self.routes.append({"path": path, "component": comp, "line": n.start_point[0] + 1})
                    child_prefix = path
            elif t == "call_expression":
                self._call(n)
            stack.extend((c, child_prefix) for c in reversed(n.named_children))

    @staticmethod
    def _ident(n: Node | None) -> str | None:
        return _t(n) if n is not None and n.type in ("identifier", "member_expression") else None

    def _attr(self, el: Node, name: str) -> Node | None:
        for a in el.named_children:
            if a.type == "jsx_attribute" and a.named_children and _t(a.named_children[0]) == name:
                return a.named_children[1] if len(a.named_children) > 1 else None
        return None

    def _attr_str(self, el: Node, name: str) -> str | None:
        v = self._attr(el, name)
        if v is None:
            return None
        if v.type == "jsx_expression" and v.named_children:
            return url_value(v.named_children[0], self.consts)
        return _str(v)

    def _jsx_route(self, el: Node, prefix: str) -> dict | None:
        path = self._attr_str(el, "path")
        if path is None:
            return None
        comp = None
        element = self._attr(el, "element")
        if element is not None:
            comp = _first_jsx_component(element)
        for key in ("component", "Component"):
            v = self._attr(el, key)
            if comp is None and v is not None and v.type == "jsx_expression" and v.named_children:
                comp = self._ident(v.named_children[0])
        return {"path": _join(prefix, path), "component": comp, "line": el.start_point[0] + 1}

    def _call(self, n: Node) -> None:
        fn = n.child_by_field_name("function")
        args_node = n.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node is not None else []) if a.type != "comment"]
        line = n.start_point[0] + 1
        if fn is None:
            return
        if fn.type == "identifier":
            name = _t(fn)
            self.calls.append({"name": name, "line": line})
            if name == "fetch" and args:
                url = url_value(args[0], self.consts)
                if _urlish(url):
                    method = _str(_pairs(args[1]).get("method")) if len(args) > 1 else None
                    self.api_calls.append({"method": (method or "GET").upper(), "url": url, "line": line, "client": "fetch"})
                return
        elif fn.type == "member_expression":
            obj, prop = fn.child_by_field_name("object"), _t(fn.child_by_field_name("property"))
            if obj is not None and obj.type in ("identifier", "this", "member_expression") and re.fullmatch(r"[\w.$]+", _t(obj)):
                self.calls.append({"name": f"{_t(obj)}.{prop}", "line": line})
            if prop in HTTP_VERBS and args:
                url = url_value(args[0], self.consts)
                if _urlish(url):
                    self.api_calls.append({"method": prop.upper(), "url": url, "line": line, "client": _t(obj)[:60]})
                    return
        # axios({ url, method }) / request({ url, method })
        if args and args[0].type == "object":
            pairs = _pairs(args[0])
            url = url_value(pairs.get("url"), self.consts)
            if _urlish(url):
                method = _str(pairs.get("method"))
                self.api_calls.append({"method": (method or "GET").upper(), "url": url, "line": line, "client": _t(fn)[:60]})


def _symbol(name: str, kind: str, decl: Node, body: Node | None, consts: dict) -> dict:
    sc = _Scanner(consts)
    if body is not None:
        sc.scan(body)
    is_class_component = kind == "class" and any(
        _t(c).endswith(("Component", "PureComponent")) for c in decl.named_children if c.type == "class_heritage")
    is_component = bool(_PASCAL.match(name)) and (sc.has_jsx or is_class_component)
    return {
        "name": name, "kind": kind,
        "is_component": is_component, "is_hook": bool(_HOOK.match(name)),
        "start_line": decl.start_point[0] + 1, "end_line": decl.end_point[0] + 1,
        "code": _t(decl)[:MAX_CODE],
        "calls": sc.calls, "jsx": sorted(sc.jsx), "api_calls": sc.api_calls, "routes": sc.routes,
    }


def _func_from_value(v: Node | None) -> Node | None:
    """`() => ...`, `function(){}`, or a wrapper call like React.memo(...)/forwardRef(...)/observer(...)."""
    if v is None:
        return None
    if v.type in FUNC_TYPES:
        return v
    if v.type == "call_expression":
        args = v.child_by_field_name("arguments")
        for a in (args.named_children if args is not None else []):
            f = _func_from_value(a)
            if f is not None:
                return f
    return None


def parse_ts(src: bytes, tsx: bool = True) -> dict:
    root = (_TSX if tsx else _TS).parse(src).root_node
    imports, reexports, exports = [], [], {}
    consts: dict[str, str] = {}
    symbols: list[dict] = []
    module_nodes: list[Node] = []

    # Pass 1: module-level string constants (used to evaluate URLs everywhere in the file).
    for n in root.named_children:
        d = n.child_by_field_name("declaration") if n.type == "export_statement" else n
        if d is not None and d.type in ("lexical_declaration", "variable_declaration"):
            for vd in d.named_children:
                if vd.type == "variable_declarator":
                    s = _str(vd.child_by_field_name("value"))
                    if s is not None:
                        consts[_t(vd.child_by_field_name("name"))] = s

    def handle_decl(d: Node, exported: bool, default: bool) -> None:
        if d.type in ("function_declaration", "generator_function_declaration"):
            name = _t(d.child_by_field_name("name")) or "default"
            symbols.append(_symbol(name, "function", d, d.child_by_field_name("body"), consts))
            if exported:
                exports["default" if default else name] = name
        elif d.type in ("class_declaration", "abstract_class_declaration", "class"):
            name = _t(d.child_by_field_name("name")) or "default"
            symbols.append(_symbol(name, "class", d, d.child_by_field_name("body"), consts))
            if exported:
                exports["default" if default else name] = name
        elif d.type in ("lexical_declaration", "variable_declaration"):
            for vd in d.named_children:
                if vd.type != "variable_declarator":
                    continue
                name = _t(vd.child_by_field_name("name"))
                value = vd.child_by_field_name("value")
                f = _func_from_value(value)
                if f is not None and re.fullmatch(r"[\w$]+", name):
                    symbols.append(_symbol(name, "function", vd, f.child_by_field_name("body"), consts))
                elif value is not None:
                    module_nodes.append(value)
                if exported:
                    exports[name] = name
        elif default:  # export default <expression>
            f = _func_from_value(d)
            if f is not None:
                fname = _t(f.child_by_field_name("name")) if f.child_by_field_name("name") is not None else "default"
                symbols.append(_symbol(fname, "function", d, f.child_by_field_name("body"), consts))
                exports["default"] = fname
            elif d.type == "identifier":
                exports["default"] = _t(d)
            elif d.type == "call_expression":  # export default connect(...)(Foo) / memo(Foo)
                idents = [a for a in d.named_children[-1].named_children if a.type == "identifier"] if d.named_children else []
                if idents:
                    exports["default"] = _t(idents[0])
                module_nodes.append(d)
            else:
                module_nodes.append(d)
        else:
            module_nodes.append(d)

    for n in root.named_children:
        if n.type == "import_statement":
            src_node = n.child_by_field_name("source")
            imp = {"source": _str(src_node), "default": None, "named": [], "namespace": None}
            clause = next((c for c in n.named_children if c.type == "import_clause"), None)
            for c in (clause.named_children if clause is not None else []):
                if c.type == "identifier":
                    imp["default"] = _t(c)
                elif c.type == "namespace_import":
                    imp["namespace"] = _t(c.named_children[-1]) if c.named_children else None
                elif c.type == "named_imports":
                    for s in c.named_children:
                        if s.type == "import_specifier":
                            nm = _t(s.child_by_field_name("name"))
                            alias = s.child_by_field_name("alias")
                            imp["named"].append([nm, _t(alias) if alias is not None else nm])
            imports.append(imp)
        elif n.type == "export_statement":
            source = _str(n.child_by_field_name("source"))
            decl = n.child_by_field_name("declaration")
            value = n.child_by_field_name("value")
            clause = next((c for c in n.named_children if c.type == "export_clause"), None)
            is_default = any(c.type == "default" for c in n.children)
            if source is not None:
                names = []
                if clause is not None:
                    for s in clause.named_children:
                        if s.type == "export_specifier":
                            nm = _t(s.child_by_field_name("name"))
                            alias = s.child_by_field_name("alias")
                            names.append([nm, _t(alias) if alias is not None else nm])
                reexports.append({"source": source, "all": clause is None, "names": names})
            elif clause is not None:
                for s in clause.named_children:
                    if s.type == "export_specifier":
                        nm = _t(s.child_by_field_name("name"))
                        alias = s.child_by_field_name("alias")
                        exports[_t(alias) if alias is not None else nm] = nm
            elif decl is not None:
                handle_decl(decl, True, is_default)
            elif value is not None:
                handle_decl(value, True, True)
        elif n.type in ("function_declaration", "generator_function_declaration", "class_declaration",
                        "abstract_class_declaration", "lexical_declaration", "variable_declaration"):
            handle_decl(n, False, False)
        elif n.type != "comment":
            module_nodes.append(n)

    mod = _Scanner(consts)
    for m in module_nodes:
        mod.scan(m)
    return {
        "imports": imports, "reexports": reexports, "exports": exports, "consts": consts,
        "symbols": symbols,
        "module": {"calls": mod.calls, "jsx": sorted(mod.jsx), "api_calls": mod.api_calls, "routes": mod.routes},
        "lines": root.end_point[0] + 1, "has_errors": root.has_error,
    }
