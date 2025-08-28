# tests/test_render_helpers_contract.py
from pathlib import Path
import types

from pytead.gen_tests import compute_expected_snapshot, compute_call_signature
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


def test_compute_call_signature_basic_function():
    entry = {
        "args_graph": [{"x": 1}, 2],
        "kwargs_graph": {"k": [1, 2]},
    }
    lines, call = compute_call_signature(
        func_name="twice",
        entry=entry,
        param_types={},    # pas de hints -> graph_to_data
        owner_class=None,
    )
    body = "\n".join(lines + [call])
    assert "graph_to_data(args_graph[0])" in body
    assert "graph_to_data(args_graph[1])" in body
    assert "kwargs_graph = graph_to_data(kwargs_graph)" in body
    assert "real_result = twice(hydrated_arg_0, hydrated_arg_1, **kwargs_graph)" in body


def test_compute_call_signature_method_with_self_and_hints():
    class Dummy:  # simulateur pour param_types
        pass

    entry = {
        "args_graph": [{"$id": 1, "self": True}, {"k": "v"}],
        "kwargs_graph": {},
    }
    # owner_class passé en *nom* (car il sera importé dans le module généré)
    lines, call = compute_call_signature(
        func_name="doit",
        entry=entry,
        param_types={"arg1": Dummy},  # un hint non-builtins -> rehydrate_from_graph
        owner_class="C",
    )
    body = "\n".join(lines + [call])
    # self_instance depuis args[0]
    assert "self_instance = rehydrate_from_graph(graph_to_data(args_graph[0]), C)" in body
    # le 1er vrai argument (hors self) utilise la classe Dummy
    assert "hydrated_arg_0 = rehydrate_from_graph(graph_to_data(args_graph[1]), Dummy)" in body
    assert "real_result = self_instance.doit(hydrated_arg_0)" in body

