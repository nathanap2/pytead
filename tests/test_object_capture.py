# tests/test_object_capture.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from pytead.tracing import trace
from pytead.storage import PickleStorage
from pytead.gen_tests import collect_entries
from pytead.rt import inject_object_args, assert_object_state, rehydrate


# --- Test fixtures -----------------------------------------------------------

class Point:
    __slots__ = ("x", "y")
    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y

def move(p: Point, dx: int, dy: int) -> Point:
    return Point(p.x + dx, p.y + dy)

def make_list(p: Point, n: int) -> list[Point]:
    return [Point(p.x + i, p.y) for i in range(n)]


# --- Tests ------------------------------------------------------------------

def test_capture_object_arg_and_result(tmp_path: Path):
    """
    When capture_objects="simple", a plain object argument and a plain object return
    value should produce:
      - entry["obj_args"] with a positional spec for index 0
      - entry["result_obj"] with the structural snapshot of the returned object
    """
    calls = tmp_path / "calls"
    wrapped = trace(
        limit=10,
        storage_dir=calls,
        storage=PickleStorage(),
        capture_objects="simple",
    )(move)

    p = Point(1, 2)
    out = wrapped(p, 2, 0)
    assert isinstance(out, Point) and (out.x, out.y) == (3, 2)

    by_func = collect_entries(calls, formats=["pickle"])
    # Find the entry for our function
    func_name = next(k for k in by_func if k.endswith(".move"))
    entry = by_func[func_name][0]

    # obj_args present for positional index 0
    assert "obj_args" in entry and "pos" in entry["obj_args"]
    pos = entry["obj_args"]["pos"]
    assert 0 in pos
    assert pos[0]["type"].endswith(".Point")
    assert pos[0]["state"] == {"x": 1, "y": 2}

    # result_obj present with expected state
    assert "result_obj" in entry
    assert entry["result_obj"]["type"].endswith(".Point")
    assert entry["result_obj"]["state"] == {"x": 3, "y": 2}


def test_list_of_objects_in_result_is_not_structurally_captured_yet(tmp_path: Path):
    """
    Current behavior: returning a container (e.g., list[Point]) does NOT populate
    'result_obj'. This documents the limitation (not a failure).
    """
    calls = tmp_path / "calls"
    wrapped = trace(
        limit=10,
        storage_dir=calls,
        storage=PickleStorage(),
        capture_objects="simple",
    )(make_list)

    p = Point(5, 7)
    out = wrapped(p, 3)  # returns [Point(5,7), Point(6,7), Point(7,7)]
    assert isinstance(out, list) and len(out) == 3 and all(isinstance(q, Point) for q in out)

    by_func = collect_entries(calls, formats=["pickle"])
    func_name = next(k for k in by_func if k.endswith(".make_list"))
    entry = by_func[func_name][0]

    # For now, 'result_obj' is absent because the top-level result is a builtin container.
    assert "result_obj" not in entry

    # However, the raw (pickled) 'result' is a real list of Point instances at load-time.
    result = entry["result"]
    assert isinstance(result, list) and all(isinstance(q, Point) for q in result)


def test_inject_object_args_helper_rehydrates_inputs():
    """
    Basic sanity-check: given a placeholder-free args tuple and an obj_args spec,
    inject_object_args must replace the targeted element with a rehydrated instance.
    """
    # Fake call where args[0] should become a Point(10, 20)
    args = ("<Point object at 0xDEADBEEF>", 123)  # the string value is irrelevant when self_type=None
    kwargs: Dict[str, Any] = {}

    obj_args = {
        "pos": {
            0: {"type": f"{Point.__module__}.Point", "state": {"x": 10, "y": 20}}
        },
        "kw": {}
    }

    new_args, new_kwargs = inject_object_args(args, kwargs, obj_args, self_type=None)
    assert isinstance(new_args[0], Point)
    assert_object_state(new_args[0], {"x": 10, "y": 20})
    # untouched tail
    assert new_args[1] == 123 and new_kwargs == {}

