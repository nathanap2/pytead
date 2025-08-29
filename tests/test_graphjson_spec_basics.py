# tests/test_graphjson_spec_basics.py
import math
import pytest

from pytead.graph_capture import capture_object_graph
from pytead.testkit import (
    graph_to_data,
    assert_match_graph_snapshot,
    sanitize_for_py_literals,
)

# ---------------------------------------------------------------------------
# Invariants locked by this spec:
#  - Aliasing of mutables is represented with {"$ref": N} in the *rendered* graph;
#    immutables are compared by value only.
#  - Non-JSON dict keys (e.g., tuples) are encoded using {"$map": [[k_graph, v_graph], ...]}.
#  - Sets/frozensets use {"$set": [...], "$frozen": bool} (deterministic ordering, data-only).
#  - Tuples may be rendered as lists; the assertion harmonizes both sides.
#  - NaN/±Inf are sanitized to None by the assertion on both sides.
#  - We do NOT reconstruct alias identity during replay; "$ref" markers remain in data
#    returned by graph_to_data (we compare structure/topology, not object identity).
# ---------------------------------------------------------------------------

def _has_ref_marker(node) -> bool:
    """Detect presence of at least one {"$ref": N} anywhere in a rendered graph."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], int):
            return True
        return any(_has_ref_marker(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_ref_marker(x) for x in node)
    return False


def test_aliasing_mutable_encoded_with_ref():
    # Two fields pointing to the *same* list → aliasing must be encoded.
    a = []
    obj = {"x": a, "y": a}
    g = capture_object_graph(obj)
    assert _has_ref_marker(g), "Expected at least one {'$ref': N} for aliasing"


def test_no_aliasing_distinct_lists_no_ref():
    # Same shape but distinct lists → no aliasing marker expected.
    obj = {"x": [], "y": []}
    g = capture_object_graph(obj)
    assert not _has_ref_marker(g), "Did not expect {'$ref': N} when no aliasing"


def test_map_tuple_keys_roundtrip_via_graph_to_data():
    # Tuple keys must round-trip via $map encoding/decoding.
    src = {(1, 2): "a", (3, (4, 5)): "b"}
    g = capture_object_graph(src)
    assert isinstance(g, dict) and "$map" in g, "Expected $map for non-JSON keys"
    back = graph_to_data(g)
    assert back == src
    assert all(isinstance(k, tuple) for k in back.keys())


def test_set_and_frozenset_roundtrip_via_graph_to_data():
    src_set = {1, (2, 3)}
    src_fset = frozenset({(10, 20), 30})
    g_set = capture_object_graph(src_set)
    g_fset = capture_object_graph(src_fset)

    dec_set = graph_to_data(g_set)
    dec_fset = graph_to_data(g_fset)

    assert isinstance(dec_set, set)
    assert isinstance(dec_fset, frozenset)
    # Content equality (tuples must survive).
    assert dec_set == src_set
    assert dec_fset == src_fset


def test_tuple_normalization_in_assertion():
    # Runtime object has tuples, expected rendered graph uses lists → assertion should pass.
    real = ((10, 10), (10, 10))
    expected_graph = [[10, 10], [10, 10]]
    assert_match_graph_snapshot(real, expected_graph)


def test_nan_and_infinities_sanitized_to_none_on_assertion():
    real = {"order": float("nan"), "pos": [1.0, float("inf")], "neg": -float("inf")}
    expected_graph = {"order": None, "pos": [1.0, None], "neg": None}
    # The assertion sanitizes both sides, so this should pass.
    assert_match_graph_snapshot(real, expected_graph)

    # Also check sanitizer idempotence / direct call contract.
    again = sanitize_for_py_literals(real)
    assert sanitize_for_py_literals(again) == again
    assert again == {"order": None, "pos": [1.0, None], "neg": None}


def test_ref_marker_preserved_by_graph_to_data():
    # We do NOT reconstruct alias identity; "$ref" stays a small dict.
    g = {"root": [{"$ref": 7}], "other": {"$ref": 7}}
    back = graph_to_data(g)
    assert back["other"] == {"$ref": 7}
    assert back["root"][0] == {"$ref": 7}


def test_aliasing_changes_graph_shape():
    # Same values, but aliasing vs no-aliasing → graphs must differ.
    a = [42]
    aliased = {"x": a, "y": a}
    separate = {"x": [42], "y": [42]}
    g1 = capture_object_graph(aliased)
    g2 = capture_object_graph(separate)
    assert g1 != g2, "Aliasing and no-aliasing must produce different graphs"

