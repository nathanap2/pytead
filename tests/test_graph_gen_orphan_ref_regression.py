# tests/test_graph_gen_orphan_ref_minimal.py
from __future__ import annotations

from pathlib import Path
import textwrap
from pytead.gen_tests import write_tests_per_func

import pytest

from pytead.errors import OrphanRefInExpected
import logging

from pytead.graph_capture import capture_object_graph

from pytead.gen_tests import (
    _assert_no_orphan_refs_in_expected,
)
from pytead.graph_utils import collect_anchor_ids, iter_bare_refs_with_paths, find_orphan_refs


def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")



def test_generator_would_inline_orphans_in_future(tmp_path):
    # Arrange identique
    mod = tmp_path / "battle.py"
    mod.write_text("def create_monster(desc):\n    return {'ok': True}\n", encoding="utf-8")
    entry = {
        "func": "battle.create_monster",
        "args_graph": [{}],                # aucun donneur portant $id=3
        "kwargs_graph": {},
        "result_graph": {"base": {"$ref": 3}, "level": 1},  # orphelin par construction
    }
    entries_by_func = {"battle.create_monster": [entry]}
    out_dir = tmp_path / "generated"

    # Nouveau contrat : on S'ATTEND à l'exception explicite
    with pytest.raises(OrphanRefInExpected):
        write_tests_per_func(entries_by_func, out_dir, import_roots=[str(tmp_path)])
    
def test_generator_emits_orphan_ref_minimal(tmp_path: Path):
    mod = tmp_path / "battle.py"
    mod.write_text("def create_monster(desc):\n    return {'ok': True}\n", encoding="utf-8")

    entry = {
        "func": "battle.create_monster",
        "args_graph": [{}],
        "kwargs_graph": {},
        "result_graph": {
            "base": {"$ref": 3},  # orphelin volontaire
            "level": 1,
        },
    }
    entries_by_func = {"battle.create_monster": [entry]}
    out_dir = tmp_path / "generated"

    with pytest.raises(Exception):  # OrphanRefInExpected
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


def test_assert_orphan_refs_detects_and_points_paths():
    expected = {"a": {"$ref": 42}}  # no donors at all
    with pytest.raises(OrphanRefInExpected) as ei:
        _assert_no_orphan_refs_in_expected(expected, donors_graphs=[], func_qualname="fqn.x")
    msg = str(ei.value)
    assert "path=$.a" in msg  # JSONPath is reported
    assert "ref=42" in msg




def test_capture_emits_ids_for_lists_and_objects():
    shared = [1, 2, 3]
    obj = {"a": shared, "b": shared}
    g = capture_object_graph(obj, max_depth=5)

    # Dans le design actuel, l’aliasing de liste produit au moins une réf orpheline.
    orphans = find_orphan_refs(g)  # -> List[(json_path, ref_id)]
    paths = {p for (p, _rid) in orphans}
    # on s’attend à voir $.a ou $.b (au moins l’un des deux)
    assert paths & {"$.a", "$.b"}

def test_collect_ids_and_iter_refs_cover_map_and_set():
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
    expected = {"x": {"$ref": 3}}
    donors = [{"$id": 1, "dummy": 0}]  # pas d'ancre 3
    assert find_orphan_refs(expected, donors) == [("$.x", 3)]

def test_collect_ids_and_iter_refs_cover_map_and_set():
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
    

from pytead.graph_capture import capture_object_graph_checked

def test_capture_checked_raises_on_list_alias():
    shared = [1, 2]
    obj = {"a": shared, "b": shared}
    with pytest.raises(Exception):  # GraphCaptureRefToUnanchored
        capture_object_graph_checked(obj)


# --- new tests reproducing real-world orphan $ref in v2 traces ---

import pytest

def test_v2_trace_orphan_ref_emits_warning_and_raises(caplog):
    """
    Reproduit un trace .gjson où result_graph contient {'$ref': 3}
    sans qu'aucune ancre '$id': 3 n'existe dans args/kwargs/result.
    On attend : warning + OrphanRefInExpected.
    """
    from pytead.gen_tests import compute_expected_snapshot
    from pytead.errors import OrphanRefInExpected

    caplog.set_level(logging.WARNING, logger="pytead.gen")

    entry = {
        "func": "battle.create_monster",
        "args_graph": [  # pas d'ancre dans les donneurs
            {"level": 36}
        ],
        "kwargs_graph": {},
        "result_graph": {
            "base": {"$ref": 3},   # <- ref orpheline
            "level": 36
        },
    }

    with pytest.raises(OrphanRefInExpected):
        _ = compute_expected_snapshot(entry, func_qualname=entry["func"])

    msgs = [rec.message for rec in caplog.records if rec.name == "pytead.gen"]
    assert any("ORPHAN_REF remains after projection" in m for m in msgs), \
        "Le pipeline doit logger un avertissement clair avant l'exception."


def test_v2_trace_ref_is_inlined_when_donor_provides_anchor():
    """
    Voie saine : si un donneur (args/kwargs) possède l'ancre {\"$id\": 3, ...},
    alors {'$ref': 3} dans result_graph doit être inliné en v1 sans $ref résiduel.
    """
    from pytead.gen_tests import compute_expected_snapshot

    entry = {
        "func": "battle.create_monster",
        "args_graph": [
            {"$id": 3, "HP": 79, "Attack": 55}  # <- ancre donneur
        ],
        "kwargs_graph": {},
        "result_graph": {
            "base": {"$ref": 3},   # <- sera inliné depuis args_graph
            "level": 36
        },
    }

    expected = compute_expected_snapshot(entry, func_qualname=entry["func"])
    # En v1 'expected', il ne doit plus y avoir de $ref et 'base' est matériel.
    assert isinstance(expected, dict)
    assert "base" in expected and isinstance(expected["base"], dict)
    assert expected["base"].get("HP") == 79
    assert expected["base"].get("Attack") == 55


import json
from pathlib import Path
import pytest

from pytead import trace
from pytead.errors import GraphJsonOrphanRef

# Selon ta version, l’API suivante peut être dans pytead.gen_tests
# (c’est celle qu’utilise "pytead gen"). Si le nom diffère chez toi,
# ajuste l’import et/ou l’exception attrapée plus bas.
from pytead.gen_tests import compute_expected_snapshot


def test_projection_leaves_internal_ref_unresolved():
    """
    Reproduit le crash 'pytead gen':
      - Graphe v2 avec une ancre interne ($id: 3) et un alias interne ($ref: 3)
      - La projection 'expected snapshot' lève une erreur signalant $.base -> ref=3
    Ce test capture le comportement défectueux côté projection.
    """
    # Entrée minimale: même forme que celle vue dans ton .gjson
    entry = {
        "trace_schema": "pytead/v2-graph",
        "timestamp": "2025-08-26T22:24:36.763557+00:00",
        "func": "battle.create_monster",
        "args_graph": [
            {  # le contenu exact des args n'a pas d'importance ici
                "$id": 1,
                "level": 36,
            }
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
            # ← alias interne vers l’ancre ci-dessus
            "base": {"$ref": 3},
            "RNG": True,
            "level": 36,
        },
    }

    # On attend aujourd’hui que la projection "expected" lève une erreur
    # du type "Unresolved {'$ref': N} ... $.base -> ref=3"
    from pytead.gen_tests import compute_expected_snapshot

    out = compute_expected_snapshot(entry, func_qualname=entry["func"])
    assert isinstance(out, dict)
    # l’alias interne $.base est matérialisé:
    assert out["base"] == {
        "HP": 35, "Attack": 55, "Defense": 40,
        "Sp. Attack": 50, "Sp. Defense": 50, "Speed": 90,
    }


def test_guardrail_should_block_cross_graph_ref(tmp_path: Path):
    """
    Documente l’attendu côté guardrail:
    si result_graph contient {'$ref': N} alors que l’ancre $id=N n’existe
    ni dans result_graph ni dans args/kwargs, GraphJsonStorage doit refuser d’écrire.
    (Ce test est "rouge" si le guardrail ne s’active pas correctement.)
    """
    # Fabrique une entrée volontairement invalide (ref orpheline)
    entry = {
        "trace_schema": "pytead/v2-graph",
        "timestamp": "2025-08-26T22:24:36.763557+00:00",
        "func": "dummy.module.identity",
        "args_graph": [{"$id": 999, "x": 1}],  # ancre ailleurs, pas 3
        "kwargs_graph": {},
        "result_graph": {"base": {"$ref": 3}},  # ← orpheline vis-à-vis de tout le bundle
    }

    from pytead.storage import GraphJsonStorage
    st = GraphJsonStorage()
    out = tmp_path / "should_not_exist.gjson"

    with pytest.raises(GraphJsonOrphanRef) as ei:
        st.dump(entry, out)

    # message informatif
    assert "ref=3" in str(ei.value)

    # et rien n’a été écrit
    assert not out.exists()


def test_assert_match_internal_alias_materialized():
    from pytead.testkit import assert_match_graph_snapshot, _normalize_for_compare  # où elle se trouve

    # On n’a pas besoin d’objets custom: une structure littérale suffit
    real = {
        "species": {
            "base_stats": {"HP": 35, "Attack": 55, "Defense": 40, "Sp. Attack": 50, "Sp. Defense": 50, "Speed": 90}
        },
        "base": {"$ref": 3},   # sera ignoré par capture si tu ne passes pas par capture,
    }
    # Comme assert_match_graph_snapshot capture via capture_object_graph(real),
    # construisons directement un graphe v2 "comme capturé" :
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

    # En contournant la capture, appelle directement la normalisation si nécessaire,
    # sinon adapte au helper qui compare déjà via capture + _normalize_for_compare.
    rn = _normalize_for_compare(real_captured)
    en = _normalize_for_compare(expected)
    assert rn["base"] == en["base"]
    
def test_normalize_tuple_marker_matches_python_tuple():
    # expected façon v2
    expected = {"$tuple": [{"$tuple": [10, 10]}, {"$tuple": [10, 10]}]}
    # réel côté runtime (list de tuples)
    real = [(10, 10), (10, 10)]
    # simulate pipeline
    from pytead.testkit import capture_object_graph, _normalize_for_compare, sanitize_for_py_literals
    real_graph = capture_object_graph(real, max_depth=2)
    real_norm = sanitize_for_py_literals(_normalize_for_compare(real_graph))
    exp_norm  = sanitize_for_py_literals(_normalize_for_compare(expected))
    assert real_norm == exp_norm
