# tests/test_graph_inline_external_refs.py
from __future__ import annotations

import pytest

# On s’appuie sur les helpers déjà présents dans gen_tests
from pytead.gen_tests import _build_ref_donor_index, _inline_external_refs_in_expected

def test_inline_replaces_ref_with_donor_anchor_simple():
    donors = [
        [{"$id": 7, "kind": "node", "v": 42}],  # args_graph
        {"k": {"$id": 2, "v": "x"}},            # kwargs_graph (bruit)
    ]
    expected = {"a": {"$ref": 7}, "b": 0}

    idx = _build_ref_donor_index(donors)
    out = _inline_external_refs_in_expected(expected, idx)

    # La ref 7 vient des donneurs et doit être inlinée (avec $id débarrassé)
    assert out["a"] == {"kind": "node", "v": 42}
    # Aucun $id ne doit rester dans la zone inlinée
    assert "$id" not in out["a"]
    # Les autres champs intacts
    assert out["b"] == 0


def test_inline_leaves_internal_aliasing_intact():
    # expected contient sa propre ancre (= aliasing interne)
    expected = {"root": {"$id": 11, "v": [1, 2]}, "alias": {"$ref": 11}}
    idx = _build_ref_donor_index([[], {}])  # donneurs vides
    out = _inline_external_refs_in_expected(expected, idx)

    # On ne "dé-aliase" pas l’aliasing interne : la ref reste
    assert out["alias"] == {"$ref": 11}
    # L’ancre interne reste inchangée (l’inline ne touche que les refs externes)
    assert out["root"]["v"] == [1, 2] and out["root"]["$id"] == 11


def test_inline_ignores_unknown_ids():
    donors = [[{"$id": 1, "keep": True}]]
    expected = {"x": {"$ref": 99}}  # inconnu des donneurs
    idx = _build_ref_donor_index(donors)
    out = _inline_external_refs_in_expected(expected, idx)

    # Ref inconnue -> laissée telle quelle (la garde fera échouer plus tard au besoin)
    assert out["x"] == {"$ref": 99}

def test_expected_internal_ref_is_inlined_before_v1():
    entry = {
        "func": "battle.create_monster",
        "args_graph": [],  # peu importe ici
        "kwargs_graph": {},
        "result_graph": {
            "$id": 1,
            "species": {
                "$id": 2,
                "base_stats": {"$id": 3, "HP": 35}
            },
            "base": {"$ref": 3},
        },
    }
    from pytead.gen_tests import compute_expected_snapshot
    exp = compute_expected_snapshot(entry, func_qualname="battle.create_monster")
    import json
    s = json.dumps(exp)
    assert "$ref" not in s and "$id" not in s  # plus de refs/id en v1

