from kg.store import MAX_ID_BYTES, MAX_INDEXED_BYTES, safe_id, safe_props


def test_short_values_unchanged():
    p = {"name": "GET /orders", "path": "src/A.java", "url": "x" * 20000}
    assert safe_props(p) == p
    assert safe_id("api:repo:src/A.java:12") == "api:repo:src/A.java:12"


def test_long_indexed_values_capped_but_url_kept():
    huge = "ğ" * 10000  # 2 bytes each in UTF-8
    p = safe_props({"name": huge, "path_norm": huge, "url": huge, "start_line": 3})
    assert len(p["name"].encode()) <= MAX_INDEXED_BYTES + 3
    assert len(p["path_norm"].encode()) <= MAX_INDEXED_BYTES + 3
    assert p["url"] == huge and p["start_line"] == 3


def test_long_id_is_deterministic_and_distinct():
    a, b = "topic:" + "a" * 5000, "topic:" + "a" * 4999 + "b"
    assert safe_id(a) == safe_id(a)
    assert safe_id(a) != safe_id(b)
    assert len(safe_id(a).encode()) <= MAX_ID_BYTES
