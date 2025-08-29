# tests/test_graph_inline_external_refs.py
from __future__ import annotations

import json
import pytest

from pytead.gen_tests import compute_expected_snapshot
from pytead.errors import OrphanRefInExpected


def _mk_entry(args_graph, kwargs_graph, result_graph):
    return {
        "func": "pkg.mod.f",
        "args_graph": args_graph,
        "kwargs_graph": kwargs_graph,
        "result_graph": result_graph,
    }


def test_external_ref_is_inlined_from_donors():
    # Donor anchor ($id=7) dans args_graph, référencée par le résultat
    entry = _mk_entry(
        args_graph=[{"$id": 7, "kind": "node", "v": 42}],
        kwargs_graph={"k": {"$id": 2, "v": "x"}},  # bruit
        result_graph={"a": {"$ref": 7}, "b": 0},
    )
    exp = compute_expected_snapshot(entry, func_qualname="pkg.mod.f")
    assert exp == {"a": {"kind": "node", "v": 42}, "b": 0}  # $id supprimés, ref inlinée


def test_internal_alias_is_dealiased_in_expected():
    # Aliasing interne : par design, l'expected est *dé-alisé*
    entry = _mk_entry(
        [],
        {},
        {"root": {"$id": 11, "v": [1, 2]}, "alias": {"$ref": 11}},
    )
    exp = compute_expected_snapshot(entry, func_qualname="pkg.mod.f")
    assert exp["root"] == {"v": [1, 2]}
    assert exp["alias"] == {"v": [1, 2]}  # plus de "$ref" en expected


def test_orphan_ref_in_expected_raises():
    # Ref inconnue des donneurs ET du résultat → doit lever
    entry = _mk_entry([], {}, {"x": {"$ref": 99}})
    with pytest.raises(OrphanRefInExpected):
        compute_expected_snapshot(entry, func_qualname="pkg.mod.f")


def test_expected_internal_ref_is_inlined_before_v1():
    entry = {
        "func": "battle.create_monster",
        "args_graph": [],
        "kwargs_graph": {},
        "result_graph": {
            "$id": 1,
            "species": {
                "$id": 2,
                "base_stats": {"$id": 3, "HP": 35},
            },
            "base": {"$ref": 3},
        },
    }
    exp = compute_expected_snapshot(entry, func_qualname="battle.create_monster")
    s = json.dumps(exp)
    assert "$ref" not in s and "$id" not in s

