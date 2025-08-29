# tests/test_graph_gen_orphan_ref_minimal.py
from __future__ import annotations

from pathlib import Path
import textwrap
import json
import logging

import pytest

from pytead.gen_tests import write_tests_per_func, compute_expected_snapshot
from pytead.errors import OrphanRefInExpected, GraphJsonOrphanRef
from pytead.graph_capture import (
    capture_object_graph,          # rendered graph (anchored -> rendered projection)
    capture_object_graph_checked,  # rendered graph + raises on orphan refs
)
from pytead.graph_utils import (
    collect_anchor_ids,
    iter_bare_refs_with_paths,
    find_orphan_refs_in_rendered,
)


def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")


def test_generator_would_inline_orphans_in_future(tmp_path: Path):
    """
    The generator should now raise explicitly when the rendered expected graph
    contains an orphan {'$ref': N} that no donor can satisfy.
    """
    mod = tmp_path / "battle.py"
    mod.write_text("def create_monster(desc):\n    return {'ok': True}\n", encoding="utf-8")

    entry = {
        "func": "battle.create_monster",
        "args_graph": [{}],  # no donor carrying $id=3
        "kwargs_graph": {},
        "result_graph": {"base": {"$ref": 3}, "level": 1},  # deliberately orphaned
    }
    entries_by_func = {"battle.create_monster": [entry]}
    out_dir = tmp_path / "generated"

    with pytest.raises(OrphanRefInExpected):
        write_tests_per_func(entries_by_func, out_dir, import_roots=[str(tmp_path)])


def test_generator_emits_orphan_ref_minimal(tmp_path: Path):
    """
    Minimal end-to-end: generation fails when expected contains an orphan ref.
    """
    mod = tmp_path / "battle.py"
    mod.write_text("def create_monster(desc):\n    return {'ok': True}\n", encoding="utf-8")

    entry = {
        "func": "battle.create_monster",
        "args_graph": [{}],
        "kwargs_graph": {},
        "result_graph": {
            "base": {"$ref": 3},  # orphan on purpose
            "level": 1,
        },
    }
    entries_by_func = {"battle.create_monster": [entry]}
    out_dir = tmp_path / "generated"

    with pytest.raises(OrphanRefInExpected):
        write_tests_per_func(entries_by_func, out_dir, import_roots=[str(tmp_path)])


def _has_bare_ref(g) -> bool:
    """Quick scan: any dict equal to {'$ref': int} anywhere?"""
    if isinstance(g, dict):
        if set(g.keys()) == {"$ref"} and isinstance(g["$ref"], int):
            return True
        return any(_has_bare_ref(v) for v in g.values())
    if isinstance(g, (list, tuple)):
        return any(_has_bare_ref(x) for x in g)
    return False



def test_capture_rendered_graph_list_alias_yields_orphan_ref():
    """
    Rendering a graph with list aliasing typically yields at least one orphan ref
    (since anchors are stripped in rendered view). We assert it is detected.
    """
    shared = [1, 2, 3]
    obj = {"a": shared, "b": shared}
    g = capture_object_graph(obj, max_depth=5)  # rendered graph
    orphans = find_orphan_refs_in_rendered(g)   # -> List[(json_path, ref_id)]
    paths = {p for (p, _rid) in orphans}
    assert paths & {"$.a", "$.b"}


def test_collect_ids_and_iter_refs_cover_map_and_set():
    """
    Low-level utilities must traverse $map/$set and expose both anchors and refs.
    """
    g = {
        "$map": [
            [{"$id": 10, "k": 1}, {"$ref": 10}],
        ],
        "$set": [{"$ref": 99}],
    }
    ids = collect_anchor_ids(g)
    assert 10 in ids

    refs = list(iter_bare_refs_with_paths(g))
    assert ("$.$map[0].value", 10) in refs
    assert ("$.$set[0]", 99) in refs


def test_find_orphan_refs_detects_missing_anchor():
    """
    Orphan ref detection must report missing anchors when donors don't provide them.
    """
    expected = {"x": {"$ref": 3}}
    donors = [{"$id": 1, "dummy": 0}]  # no $id=3
    assert find_orphan_refs_in_rendered(expected, donors) == [("$.x", 3)]


def test_capture_checked_raises_on_list_alias():
    """
    The 'checked' capture should raise when rendered projection leaves orphan refs.
    """
    shared = [1, 2]
    obj = {"a": shared, "b": shared}
    with pytest.raises(Exception):  # GraphCaptureRefToUnanchored
        capture_object_graph_checked(obj)


# --- tests reproducing real-world orphan $ref in anchored traces ---

def test_anchored_trace_orphan_ref_emits_warning_and_raises(caplog):
    """
    Reproduces an anchored trace where result_graph contains {'$ref': 3}
    but no donor (args/kwargs/result) provides '$id': 3.
    Expect: warning on 'pytead.gen' + OrphanRefInExpected.
    """
    caplog.set_level(logging.WARNING, logger="pytead.gen")

    entry = {
        "func": "battle.create_monster",
        "args_graph": [  # no anchor in donors
            {"level": 36}
        ],
        "kwargs_graph": {},
        "result_graph": {
            "base": {"$ref": 3},   # orphan by construction
            "level": 36
        },
    }

    with pytest.raises(OrphanRefInExpected):
        _ = compute_expected_snapshot(entry, func_qualname=entry["func"])

    msgs = [rec.message for rec in caplog.records if rec.name == "pytead.gen"]
    assert any("ORPHAN_REF remains after projection" in m for m in msgs), \
        "The pipeline should log a clear warning before throwing."


def test_anchored_trace_ref_is_inlined_when_donor_provides_anchor():
    """
    Healthy path: if a donor (args/kwargs) carries the anchor {"$id": 3, ...},
    then {'$ref': 3} in result_graph must be inlined in the rendered expected.
    """
    entry = {
        "func": "battle.create_monster",
        "args_graph": [
            {"$id": 3, "HP": 79, "Attack": 55}  # donor anchor
        ],
        "kwargs_graph": {},
        "result_graph": {
            "base": {"$ref": 3},   # will be inlined from args_graph
            "level": 36
        },
    }

    expected = compute_expected_snapshot(entry, func_qualname=entry["func"])
    # In the rendered expected, there must be no $ref and 'base' must be materialized.
    assert isinstance(expected, dict)
    assert "base" in expected and isinstance(expected["base"], dict)
    assert expected["base"].get("HP") == 79
    assert expected["base"].get("Attack") == 55


def test_projection_internal_alias_is_materialized():
    """
    Internal aliasing inside result_graph should be materialized in the rendered expected.
    """
    entry = {
        "trace_schema": "pytead/anchored-graph",
        "timestamp": "2025-08-26T22:24:36.763557+00:00",
        "func": "battle.create_monster",
        "args_graph": [
            {"$id": 1, "level": 36},
        ],
        "kwargs_graph": {},
        "result_graph": {
            "$id": 1,
            "species": {
                "$id": 2,
                "base_stats": {
                    "$id": 3,
                    "HP": 35,
                    "Attack": 55,
                    "Defense": 40,
                    "Sp. Attack": 50,
                    "Sp. Defense": 50,
                    "Speed": 90,
                },
            },
            # internal alias to the anchor above
            "base": {"$ref": 3},
            "RNG": True,
            "level": 36,
        },
    }

    out = compute_expected_snapshot(entry, func_qualname=entry["func"])
    assert isinstance(out, dict)
    # The internal alias $.base should be materialized (no '$ref' left).
    assert out["base"] == {
        "HP": 35, "Attack": 55, "Defense": 40,
        "Sp. Attack": 50, "Sp. Defense": 50, "Speed": 90,
    }
    s = json.dumps(out)
    assert "$ref" not in s and "$id" not in s


def test_guardrail_should_block_cross_graph_ref(tmp_path: Path):
    """
    Storage guardrail: if result_graph contains {'$ref': N} and **no** graph in
    the bundle (args/kwargs/result) provides '$id': N, GraphJsonStorage.dump must refuse.
    """
    # Build an intentionally invalid entry (orphan reference)
    entry = {
        "trace_schema": "pytead/anchored-graph",
        "timestamp": "2025-08-26T22:24:36.763557+00:00",
        "func": "dummy.module.identity",
        "args_graph": [{"$id": 999, "x": 1}],  # unrelated anchor; not 3
        "kwargs_graph": {},
        "result_graph": {"base": {"$ref": 3}},  # orphan w.r.t. the whole bundle
    }

    from pytead.storage import GraphJsonStorage

    st = GraphJsonStorage()
    out = tmp_path / "should_not_exist.gjson"

    with pytest.raises(GraphJsonOrphanRef) as ei:
        st.dump(entry, out)

    # informative message
    assert "ref=3" in str(ei.value)

    # and nothing was written
    assert not out.exists()


def test_assert_match_internal_alias_materialized():
    """
    Runtime comparison helper should treat internal aliasing as materialized equivalence.
    """
    from pytead.testkit import assert_match_graph_snapshot, _normalize_for_compare  # whereabouts may differ

    # No custom objects needed: a literal structure is enough
    real_captured = {
        "$id": 1,
        "species": {
            "$id": 2,
            "base_stats": {
                "$id": 3,
                "HP": 35, "Attack": 55, "Defense": 40,
                "Sp. Attack": 50, "Sp. Defense": 50, "Speed": 90
            }
        },
        "base": {"$ref": 3},
        "RNG": True
    }

    expected = {
        "RNG": True,
        "species": {
            "base_stats": {"HP": 35, "Attack": 55, "Defense": 40, "Sp. Attack": 50, "Sp. Defense": 50, "Speed": 90}
        },
        "base": {"HP": 35, "Attack": 55, "Defense": 40, "Sp. Attack": 50, "Sp. Defense": 50, "Speed": 90}
    }

    # If bypassing capture, compare through the same normalization path
    rn = _normalize_for_compare(real_captured)
    en = _normalize_for_compare(expected)
    assert rn["base"] == en["base"]


def test_normalize_tuple_marker_matches_python_tuple():
    """
    The tuple marker in the expected (rendered) must match a Python tuple captured at runtime.
    """
    # expected (rendered-ish)
    expected = {"$tuple": [{"$tuple": [10, 10]}, {"$tuple": [10, 10]}]}
    # actual at runtime (list of tuples)
    real = [(10, 10), (10, 10)]
    # simulate the pipeline
    from pytead.testkit import capture_object_graph as tk_capture_rendered, _normalize_for_compare, sanitize_for_py_literals

    real_graph = tk_capture_rendered(real, max_depth=2)
    real_norm = sanitize_for_py_literals(_normalize_for_compare(real_graph))
    exp_norm = sanitize_for_py_literals(_normalize_for_compare(expected))
    assert real_norm == exp_norm
