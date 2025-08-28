# tests/test_render_graph_snapshot_contract.py
from __future__ import annotations
from pathlib import Path

from pytead.gen_tests import render_graph_snapshot_test_body

def _write(path: Path, src: str) -> None:
    path.write_text(src, encoding="utf-8")


def test_render_graph_snapshot_body_compiles_with_donors(tmp_path: Path):
    # 1) Mini-module réaliste
    mod = tmp_path / "mymod.py"
    _write(mod, """
def twice(x):
    return {"res": [x, x]}
""")
    # Rendez-le importable
    import sys
    sys.path.insert(0, str(tmp_path))

    # 2) Trace qui nécessite l'inline: expected réfère à un id fourni par les args
    entry = {
        "args_graph": [{"$id": 5, "val": 21}],
        "kwargs_graph": {},
        "result_graph": {"from_arg": {"$ref": 5}, "note": "ok"},
    }

    # 3) Pas d’annotations -> pas de rehydrate_from_graph attendu
    body = render_graph_snapshot_test_body(
        func_name="twice",
        entry=entry,
        param_types={},      # pas de hints -> fallback shell
        owner_class=None,
    )

    # 4) Le corps doit contenir nos helpers "généraux" et être compilable
    assert "assert_match_graph_snapshot" in body
    assert "graph_to_data" in body
    assert "rehydrate_from_graph" not in body  # pas de types => pas de rehydrate

    # Compile à blanc (syntaxe)
    compile(body, "<generated_test>", "exec")


def test_render_graph_snapshot_body_uses_rehydrate_when_types_present(tmp_path: Path):
    # 1) Mini-module avec un type utilisateur
    mod = tmp_path / "mymod2.py"
    _write(mod, """
class Thing:
    def __init__(self, val: int):
        self.val = val

def use(t: "Thing"):
    return {"ok": True}
""")
    import sys
    sys.path.insert(0, str(tmp_path))

    # 2) Trace minimale
    entry = {
        "args_graph": [{"$id": 1, "val": 7}],
        "kwargs_graph": {},
        "result_graph": {"ok": True},
    }

    # 3) On fournit un type pour le 1er param -> doit pousser rehydrate_from_graph
    class Thing:  # stub local suffit pour l'AST; on ne va pas exécuter le code
        pass

    body = render_graph_snapshot_test_body(
        func_name="use",
        entry=entry,
        param_types={"t": Thing},  # force l'usage de rehydrate_from_graph
        owner_class=None,
    )

    assert "rehydrate_from_graph" in body
    # Compile à blanc (syntaxe)
    compile(body, "<generated_test>", "exec")


def test_capture_map_is_anchored_and_ref_inlined():
    # 👉 Contrat IR (V2) : on vérifie la présence des ancres sur un $map
    from pytead.graph_capture import capture_object_graph_v2
    d = {(1, 2): "a", (3, 4): "b"}
    # alias (expected to produce a $ref initially)
    alias = d
    g_d = capture_object_graph_v2(d)
    g_alias = capture_object_graph_v2(alias)
    # d doit contenir "$id" ET alias doit être {"$ref": id}
    from pytead.graph_utils import collect_anchor_ids, find_orphan_refs
    ids = collect_anchor_ids(g_d)
    assert ids, "map should carry an $id anchor"
    # Si on compose un expected = {"x": {"$ref": id}}, find_orphan_refs(..., donors=[g_d]) doit être vide
    expected = {"x": g_alias}  # g_alias est de la forme {"$ref": id}
    assert not find_orphan_refs(expected, donors_graphs=[g_d]), "no orphan ref w/ anchored $map"


def test_capture_list_anchor_and_cycle_safe():
    # 👉 Contrat IR (V2) : les listes portent une ancre et les cycles sont sûrs
    a = [1, 2]
    a.append(a)  # cycle
    from pytead.graph_capture import capture_object_graph_v2
    g = capture_object_graph_v2(a)
    # ancre présente
    from pytead.graph_utils import collect_anchor_ids, validate_graph
    assert collect_anchor_ids(g), "list wrapper must carry an $id"
    assert not validate_graph(g), "no orphan refs even with self-cycle"

