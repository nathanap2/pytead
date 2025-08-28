# tests/test_graphjson_refs_guard.py
import pytest

from pytead.testkit import assert_match_graph_snapshot

def test_assertion_fails_on_orphan_ref_in_expected():
    # Real value has two identical lists (no aliasing visible here).
    real = ([1, 2], [1, 2])

    # Expected snapshot cheats with an orphan ref to "2" (no {"$id": 2} anchor provided).
    expected_graph = [[1, 2], {"$ref": 2}]

    with pytest.raises(AssertionError):
        assert_match_graph_snapshot(real, expected_graph)
        
from pytead.testkit import graph_to_data

def sum_vals(x, y):
    # A trivial function that expects plain dicts with a 'val' field.
    return x["val"] + y["val"]

def test_inputs_orphan_ref_in_args_breaks_call():
    # x is a proper dict graph
    x_graph = {"$map": [["val", 3]]}

    # y is an orphan ref (no "$id": 2 anchor anywhere in the same graph)
    y_graph = {"$ref": 2}

    x = graph_to_data(x_graph)  # -> {"val": 3}
    y = graph_to_data(y_graph)  # -> {"$ref": 2} (still a dict, not what the function expects)

    with pytest.raises((KeyError, TypeError, AttributeError)):
        _ = sum_vals(x, y)
        
def test_inputs_orphan_ref_is_preserved_precondition():
    # Orphan ref (no "$id": 2 anchor anywhere)
    y_graph = {"$ref": 2}
    y = graph_to_data(y_graph)

    # PRECONDITION: orphan refs must not be auto-resolved
    # If this assertion fails, your graph_to_data (or a wrapper) is inlining orphans.
    assert isinstance(y, dict), f"graph_to_data returned non-dict for orphan ref: {y!r}"
    assert "$ref" in y and "val" not in y, f"Orphan ref unexpectedly resolved: {y!r}"


def test_inputs_orphan_ref_in_args_breaks_call_strict():
    def sum_vals(x, y):
        return x["val"] + y["val"]  # must error if y is an orphan-ref dict

    x_graph = {"$map": [["val", 3]]}  # proper small dict graph
    y_graph = {"$ref": 2}             # orphan ref, no anchor

    x = graph_to_data(x_graph)  # -> {"val": 3}
    y = graph_to_data(y_graph)  # -> should remain {"$ref": 2}

    # PRECONDITION again (so the failure is informative, not silent):
    assert isinstance(y, dict) and "$ref" in y and "val" not in y, f"Unexpected y: {y!r}"

    with pytest.raises((KeyError, TypeError, AttributeError)):
        _ = sum_vals(x, y)

