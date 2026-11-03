"""pom.xml, package.json, tsconfig.json and Spring application config files."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import yaml


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _txt(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    x = el.find(path)
    return x.text.strip() if x is not None and x.text else None


def parse_pom(src: bytes) -> dict:
    root = ET.fromstring(src)
    _strip_ns(root)
    parent = root.find("parent")
    pg, pa, pv = _txt(parent, "groupId"), _txt(parent, "artifactId"), _txt(parent, "version")
    group = _txt(root, "groupId") or pg
    version = _txt(root, "version") or pv
    props = {"project.groupId": group, "project.version": version, "pom.groupId": group,
             "project.parent.groupId": pg, "project.parent.version": pv}
    props_el = root.find("properties")
    for p in (list(props_el) if props_el is not None else []):
        if isinstance(p.tag, str) and p.text:
            props[p.tag] = p.text.strip()

    def sub(v: str | None) -> str | None:
        if v is None:
            return None
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1)) or m.group(0), v)

    deps = []
    for d in root.findall("./dependencies/dependency"):
        deps.append({"group": sub(_txt(d, "groupId")), "artifact": sub(_txt(d, "artifactId")),
                     "version": sub(_txt(d, "version")), "scope": _txt(d, "scope") or "compile"})
    return {
        "group": group, "artifact": _txt(root, "artifactId"), "version": version,
        "packaging": _txt(root, "packaging") or "jar", "name": _txt(root, "name"),
        "parent": {"group": pg, "artifact": pa, "version": pv} if pa else None,
        "modules": [m.text.strip() for m in root.findall("./modules/module") if m.text],
        "dependencies": deps,
    }


def parse_package_json(src: bytes) -> dict:
    data = json.loads(src.decode("utf-8-sig"))
    deps = [{"name": k, "version": v, "scope": "compile"} for k, v in (data.get("dependencies") or {}).items()]
    deps += [{"name": k, "version": v, "scope": "dev"} for k, v in (data.get("devDependencies") or {}).items()]
    return {"name": data.get("name"), "version": data.get("version"), "dependencies": deps}


def _strip_json_comments(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True; out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))  # trailing commas


def parse_tsconfig(src: bytes) -> dict:
    data = json.loads(_strip_json_comments(src.decode("utf-8-sig")))
    co = data.get("compilerOptions") or {}
    return {"baseUrl": co.get("baseUrl"), "paths": co.get("paths") or {}, "extends": data.get("extends")}


def _flatten(d, prefix: str = "") -> dict[str, str]:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
    return out


def parse_spring_config(src: bytes, is_properties: bool) -> dict:
    """Return config keys (never values) plus spring.application.name and profiles."""
    text = src.decode("utf-8", errors="replace")
    docs: list[dict] = []
    if is_properties:
        flat = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line[0] in "#!":
                continue
            m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)", line)
            if m:
                flat[m.group(1)] = m.group(2)
        docs.append(flat)
    else:
        for d in yaml.safe_load_all(text):
            docs.append(_flatten(d) if isinstance(d, dict) else {})
    keys, app_name, profiles = set(), None, set()
    for flat in docs:
        keys |= set(flat)
        if "spring.application.name" in flat and app_name is None:
            app_name = str(flat["spring.application.name"])
        for pk in ("spring.config.activate.on-profile", "spring.profiles"):
            if pk in flat:
                profiles.add(str(flat[pk]))
    return {"keys": sorted(keys), "application_name": app_name, "profiles": sorted(profiles)}
