# tests/test_render_helpers_contract.py
from pathlib import Path
import types

from pytead.gen_tests import compute_expected_snapshot
from pytead.errors import OrphanRefInExpected


def test_compute_expected_snapshot_inlines_from_donors():
    entry = {
        "args_graph": [{"$id": 5, "val": 21}],
        "kwargs_graph": {},
        "result_graph": {"from_arg": {"$ref": 5}, "note": "ok"},
    }
    expected = compute_expected_snapshot(entry, func_qualname="mymod.twice")
    # après inline: plus de {"$ref": 5} sous from_arg
    assert isinstance(expected, dict)
    assert "from_arg" in expected
    assert isinstance(expected["from_arg"], dict)
    assert "$ref" not in expected["from_arg"]


def test_compute_expected_snapshot_raises_on_orphan():
    entry = {
        "args_graph": [{}],
        "kwargs_graph": {},
        "result_graph": {"x": {"$ref": 999}},
    }
    try:
        compute_expected_snapshot(entry, func_qualname="mymod.broken")
    except OrphanRefInExpected as e:
        msg = str(e)
        assert "orphan" in msg.lower()
        assert "999" in msg
    else:
        raise AssertionError("expected OrphanRefInExpected")



