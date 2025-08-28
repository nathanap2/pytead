# tests/test_regressions_small.py
import pytest

def test_capture_depth_keeps_scalars_as_ints():
    """
    Repro for the old bug where scalars at depth==0 were stringified via repr(...).
    With the fix (check `_is_scalar` BEFORE the depth guard), captured ints stay ints.
    """
    from pytead.testkit import capture_object_graph

    real = [(10, 10), (10, 10)]
    g = capture_object_graph(real, max_depth=2)

    # v1 projection: top is a Python list; first element is a Python tuple.
    assert isinstance(g, list)
    assert isinstance(g[0], tuple)
    # Critical: elements are ints (not "10")
    assert g[0] == (10, 10)
    assert all(isinstance(x, int) for x in g[0])


def test_normalizer_unwraps_list_ref_and_matches_tuple_markers():
    """
    Repro for list-level aliasing: the second element is a {'$ref': N} pointing
    to the first tuple. The normalizer should "de-alias" it so it matches
    the expected v2-style tuple markers.
    """
    from pytead.testkit import _normalize_for_compare, sanitize_for_py_literals

    # v2-like expected: tuple of two tuples (10,10)
    expected = {"$tuple": [{"$tuple": [10, 10]}, {"$tuple": [10, 10]}]}

    # v1-like real graph with list-level aliasing:
    # first element is a tuple marker, second is a bare ref to its (stripped) $id.
    real = [{"$tuple": [10, 10]}, {"$ref": 1}]

    exp_norm = sanitize_for_py_literals(_normalize_for_compare(expected))
    real_norm = sanitize_for_py_literals(_normalize_for_compare(real))

    assert real_norm == exp_norm  # used to fail before the "unwrap local list refs" pass


def test_graph_to_data_set_with_ref_element_is_hashable():
    """
    Repro for the TypeError: sets containing {'$ref': N} were unhashable during
    decoding. With the fix, refs become ('__ref__', N) in key-context and the set
    is materialized without error.
    """
    from pytead.testkit import graph_to_data

    node = {"$set": [{"$ref": 14}], "$frozen": False}
    s = graph_to_data(node)

    # Should not raise, and should be a set containing a hashable marker.
    assert isinstance(s, set)
    assert ("__ref__", 14) in s


@pytest.mark.parametrize(
    "node, expected",
    [
        # Non-frozen set becomes set(...)
        ({"$set": [1, 2, 3], "$frozen": False}, {1, 2, 3}),
        # Frozen set becomes frozenset(...)
        ({"$set": [1, 2, 3], "$frozen": True}, frozenset({1, 2, 3})),
    ],
)
def test_graph_to_data_set_basic_shapes(node, expected):
    """Sanity checks for '$set' decoding after the ref/hashability fix."""
    from pytead.testkit import graph_to_data

    res = graph_to_data(node)
    assert type(res) is type(expected)
    assert res == expected

