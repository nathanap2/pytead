# tests/test_graph_to_data_contract.py
from __future__ import annotations
from pytead.testkit import graph_to_data

def test_graph_to_data_map_and_set_roundtrip_shapes():
    # --- 1) Cas $map : clés non JSON-hashables redeviennent hashables ---
    g_map = {
        "$map": [
            [[1, 2], {"v": 9}],               # clé "liste" -> tuple
            [{"$set": [1, 2]}, {"ok": True}], # clé "set" -> frozenset (ou tuple fallback)
        ]
    }
    d = graph_to_data(g_map)

    # La clé [1,2] devient (1,2)
    assert (1, 2) in d
    assert d[(1, 2)] == {"v": 9}

    # La clé {"$set":[1,2]} devient frozenset({1,2}) (ou tuple fallback)
    if frozenset({1, 2}) in d:
        assert d[frozenset({1, 2})] == {"ok": True}
    else:
        assert tuple(sorted({1, 2})) in d
        assert d[tuple(sorted({1, 2}))] == {"ok": True}

    # --- 2) Cas $set : le nœud set est seul au niveau racine ---
    g_set = {"$set": [1, 2, 2], "$frozen": False}
    s = graph_to_data(g_set)
    # Tolérons les trois implémentations possibles selon la branche :
    #   - set({1,2}), frozenset({1,2}), ou fallback en liste triée
    assert isinstance(s, (set, frozenset, list))
    if isinstance(s, (set, frozenset)):
        assert s == {1, 2}
    else:
        assert s == sorted(s) and s == [1, 2]  # liste triée sans doublons


from pathlib import Path
from pytead.gen_tests import write_tests_per_func

def _write(p: Path, src: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")

def test_write_tests_per_func_emits_owner_import_and_self_rehydrate(tmp_path: Path):
    # 1) Mini module avec une classe et une méthode
    mod = tmp_path / "mymod.py"
    _write(mod, """
class C:
    def m(self, x):
        return {"x": x}
""")

    # 2) Trace 'graph-json' pour une méthode : args_graph[0] = self capturé
    entry = {
        "func": "mymod.C.m",
        "args_graph": [ {"$id": 1, "state": {"dummy": 0}},  {"$id": 2, "x": 42} ],
        "kwargs_graph": {},
        "result_graph": {"x": {"$ref": 2}},  # attendu : inline/compare propre
    }
    entries_by_func = {"mymod.C.m": [entry]}

    # 3) Génération d’un fichier par fonction
    out_dir = tmp_path / "generated"
    write_tests_per_func(entries_by_func, out_dir, import_roots=[str(tmp_path)])

    # 4) On lit le fichier et on vérifie les invariants de génération
    #    - import "from mymod import C"
    #    - présence de la ligne "self_instance = rehydrate_from_graph(..., C)"
    files = list(out_dir.glob("test_mymod_C_m_snapshots.py"))
    assert files, "Le fichier attendu n'a pas été généré"
    txt = files[0].read_text(encoding="utf-8")

    assert "from mymod import C" in txt
    assert "self_instance = rehydrate_from_graph(" in txt
    assert ".m(" in txt  # l’appel se fait via la méthode


from pytead.graph_utils import find_orphan_refs_in_rendered

def test_find_orphan_refs_uses_kwargs_as_donors():
    expected = {"x": {"$ref": 7}}
    donors = [{"$id": 7, "k": "in_kwargs"}]  # simulateur de kwargs_graph
    assert find_orphan_refs_in_rendered(expected, donors) == []

def test_find_orphan_refs_uses_expected_internal_anchor():
    expected = {"x": {"$ref": 7}, "anchor": {"$id": 7, "v": 1}}
    donors = []  # aucun donor externe
    assert find_orphan_refs_in_rendered(expected, donors) == []

def test_find_orphan_refs_reports_true_orphan():
    expected = {"x": {"$ref": 9}}
    donors = [{"$id": 8, "k": "wrong"}]
    orphans = find_orphan_refs_in_rendered(expected, donors)
    # Le path précis peut varier selon l’implémentation; on veut au moins le (path, 9)
    assert any(p.endswith(".x") and rid == 9 for (p, rid) in orphans) or \
           any(p in ("$.x", "$.x") and rid == 9 for (p, rid) in orphans)


from pathlib import Path
from pytead.gen_tests import write_tests_per_func
import logging

def _write(p: Path, src: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")

def test_write_tests_per_func_imports_param_types_and_hydrates(tmp_path: Path):
    mod = tmp_path / "mymod_typed.py"
    _write(mod, """
class Foo:
    def __init__(self, val: int) -> None:
        self.val = val

def use(foo: 'Foo', n: int):
    # Le contenu importe peu, on teste le *rendu* du fichier de test
    return {"ok": True}
""")

    # Trace graph-json : l’arg 0 (Foo) doit déclencher rehydrate(..., Foo)
    entry = {
        "func": "mymod_typed.use",
        "args_graph": [ {"$id": 1, "val": 7},  3 ],
        "kwargs_graph": {},
        "result_graph": {"ok": True},
    }
    entries_by_func = {"mymod_typed.use": [entry]}

    out_dir = tmp_path / "generated"
    write_tests_per_func(entries_by_func, out_dir, import_roots=[str(tmp_path)])

    files = list(out_dir.glob("test_mymod_typed_use_snapshots.py"))
    assert files, "Fichier non généré"
    txt = files[0].read_text(encoding="utf-8")

    # Import de la fonction + de Foo (via introspection des annotations)
    assert "from mymod_typed import use" in txt
    assert "from mymod_typed import Foo" in txt
    # Rehydratation typée sur l’argument 0
    assert "rehydrate_from_graph(graph_to_data(args_graph[0]), Foo)" in txt


from pytead.graph_capture import capture_object_graph


def test_capture_logs_warning_when_ref_emitted_before_anchor(caplog):
    caplog.set_level(logging.WARNING, logger="pytead.graph_capture")

    shared = [1, 2]
    obj = {"a": shared, "b": shared}
    _ = capture_object_graph(obj, max_depth=5)

    msgs = [rec.message for rec in caplog.records if rec.name == "pytead.graph_capture"]
    # Message robuste : on accepte les variantes (“without an …” / “without a surviving …”)
    assert any(("Emitting $ref" in m) and ("'$id' anchor" in m) for m in msgs)

from pathlib import Path
import importlib.util
import sys
from pytead.gen_tests import write_tests_per_func

def _write(p: Path, src: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")

def _import_from_path(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

def test_generated_snapshot_module_is_importable(tmp_path: Path):
    # Module trivial
    mod = tmp_path / "mymod2.py"
    _write(mod, "def f(x):\n    return {'x': x}\n")

    entry = {
        "func": "mymod2.f",
        "args_graph": [1],
        "kwargs_graph": {},
        "result_graph": {"x": 1},
    }
    entries_by_func = {"mymod2.f": [entry]}

    out_dir = tmp_path / "generated"
    write_tests_per_func(entries_by_func, out_dir, import_roots=[str(tmp_path)])

    fpaths = list(out_dir.glob("test_mymod2_f_snapshots.py"))
    assert fpaths, "Fichier généré manquant"

    # Le module de test doit être importable (imports valides, bootstrap sys.path OK)
    _ = _import_from_path("generated_test_mymod2_f", fpaths[0])
    
def test_normalize_keeps_scalar_types():
    from pytead.testkit import _normalize_for_compare  # adapte l'import si besoin
    # 1) Wrappers ne doivent pas convertir 10 -> "10" ni l'inverse
    ir = {"$tuple": [10, 10], "$id": 2}
    norm = _normalize_for_compare(ir)
    assert norm == [10, 10]
    assert all(isinstance(x, int) for x in norm)

    # 2) Si des strings sont réellement présentes, on les garde
    ir2 = {"$list": ["10", "10"]}
    norm2 = _normalize_for_compare(ir2)
    assert norm2 == ["10", "10"]
    assert all(isinstance(x, str) for x in norm2)
