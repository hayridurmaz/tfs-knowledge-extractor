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
