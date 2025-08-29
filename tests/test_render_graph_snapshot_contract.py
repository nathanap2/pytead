# tests/test_render_graph_snapshot_contract.py
from __future__ import annotations
from pathlib import Path

from pytead.gen_tests import render_graph_snapshot_test_body


def _write(path: Path, src: str) -> None:
    path.write_text(src, encoding="utf-8")


def test_render_graph_snapshot_body_compiles_with_donors(tmp_path: Path):
    # 1) Minimal realistic module
    mod = tmp_path / "mymod.py"
    _write(
        mod,
        """
def twice(x):
    return {"res": [x, x]}
""",
    )
    # Make it importable
    import sys

    sys.path.insert(0, str(tmp_path))

    # 2) Trace that requires inlining: expected references an id provided by args
    entry = {
        "args_graph": [{"$id": 5, "val": 21}],
        "kwargs_graph": {},
        "result_graph": {"from_arg": {"$ref": 5}, "note": "ok"},
    }

    # 3) No annotations -> no rehydrate_from_graph expected
    body = render_graph_snapshot_test_body(
        func_name="twice",
        entry=entry,
        param_types={},  # no hints -> fallback shell
        owner_class=None,
    )

    # 4) The body must include our generic helpers and be compilable
    assert "assert_match_graph_snapshot" in body
    assert "graph_to_data" in body
    assert "rehydrate_from_graph" not in body  # no types => no rehydrate

    # Syntax-only compile
    compile(body, "<generated_test>", "exec")


def test_render_graph_snapshot_body_uses_rehydrate_when_types_present(tmp_path: Path):
    # 1) Minimal module with a user type
    mod = tmp_path / "mymod2.py"
    _write(
        mod,
        """
class Thing:
    def __init__(self, val: int):
        self.val = val

def use(t: "Thing"):
    return {"ok": True}
""",
    )
    import sys

    sys.path.insert(0, str(tmp_path))

    # 2) Minimal trace
    entry = {
        "args_graph": [{"$id": 1, "val": 7}],
        "kwargs_graph": {},
        "result_graph": {"ok": True},
    }

    # 3) Provide a type for the first param -> must trigger rehydrate_from_graph
    class Thing:  # local stub is enough for AST; we won't execute the code
        pass

    body = render_graph_snapshot_test_body(
        func_name="use",
        entry=entry,
        param_types={"t": Thing},  # force using rehydrate_from_graph
        owner_class=None,
    )

    assert "rehydrate_from_graph" in body
    # Syntax-only compile
    compile(body, "<generated_test>", "exec")


def test_capture_map_is_anchored_and_ref_inlined():
    # Anchored graph contract: $map carries an anchor; inline from donors works
    from pytead.graph_capture import capture_anchored_graph

    d = {(1, 2): "a", (3, 4): "b"}
    # alias (expected to produce a $ref initially)
    alias = d
    g_d = capture_anchored_graph(d)
    g_alias = capture_anchored_graph(alias)

    # d must carry an "$id" and alias must be {"$ref": id}
    from pytead.graph_utils import collect_anchor_ids, find_orphan_refs_in_rendered

    ids = collect_anchor_ids(g_d)
    assert ids, "map should carry an $id anchor"

    # If we compose expected = {"x": {"$ref": id}}, checking against donors=[g_d] must yield no orphans
    expected = {"x": g_alias}  # g_alias is of the form {"$ref": id}
    assert not find_orphan_refs_in_rendered(
        expected, donors_graphs=[g_d]
    ), "no orphan ref with anchored $map donor"


def test_capture_list_anchor_and_cycle_safe():
    # Anchored graph contract: lists carry an anchor and cycles are safe
    a = [1, 2]
    a.append(a)  # self-cycle

    from pytead.graph_capture import capture_anchored_graph

    g = capture_anchored_graph(a)

    # Anchor present; no orphan ref even with a self-cycle
    from pytead.graph_utils import collect_anchor_ids, validate_graph

    assert collect_anchor_ids(g), "list wrapper must carry an $id"
    assert not validate_graph(g), "no orphan refs even with self-cycle"

