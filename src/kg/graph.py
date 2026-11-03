"""In-memory graph that the pipeline builds and the store writes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Labels a node may gain or lose between runs; the store removes them before re-applying.
OPTIONAL_LABELS = (
    "Interface", "Enum", "Record", "Annotation", "JpaEntity", "Controller", "FeignClient",
    "SpringRepository", "Component", "Hook", "Test",
)

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokens(*texts: str | None) -> str:
    """Split identifiers into lowercase words so full-text search finds 'order service' in OrderService."""
    words: list[str] = []
    for t in texts:
        if t:
            words.extend(w.lower() for w in _CAMEL.findall(t))
    return " ".join(dict.fromkeys(words))


@dataclass
class Node:
    id: str
    label: str  # primary kind label
    props: dict
    extra_labels: set[str] = field(default_factory=set)


@dataclass
class Edge:
    type: str
    src: str
    dst: str
    props: dict = field(default_factory=dict)


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}

    def node(self, id: str, label: str, extra_labels: set[str] | None = None, **props) -> Node:
        props = {k: v for k, v in props.items() if v is not None and v != [] and v != ""}
        existing = self.nodes.get(id)
        if existing:
            existing.props.update(props)
            if extra_labels:
                existing.extra_labels |= extra_labels
            return existing
        n = Node(id, label, props, set(extra_labels or ()))
        self.nodes[id] = n
        return n

    def edge(self, type: str, src: str, dst: str, **props) -> Edge:
        """Add an edge; repeated (type, src, dst) merges: counts add up, the best confidence wins."""
        key = (type, src, dst)
        e = self.edges.get(key)
        if e is None:
            e = Edge(type, src, dst, {k: v for k, v in props.items() if v is not None})
            self.edges[key] = e
            return e
        if "count" in props:
            e.props["count"] = e.props.get("count", 0) + props["count"]
        if "confidence" in props:
            e.props["confidence"] = max(e.props.get("confidence", 0), props["confidence"])
        if "lines" in props:
            e.props["lines"] = sorted(set(e.props.get("lines", [])) | set(props["lines"]))[:20]
        return e

    def finalize(self) -> None:
        """Drop edges whose endpoints were never created, and stamp each edge with its source repo."""
        for key in [k for k, e in self.edges.items() if e.src not in self.nodes or e.dst not in self.nodes]:
            del self.edges[key]
        for e in self.edges.values():
            e.props["repo"] = self.nodes[e.src].props.get("repo", "")
