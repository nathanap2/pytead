# tests/test_graph_diag_scan.py
from __future__ import annotations
from pathlib import Path
from collections import defaultdict

from pytead.storage import iter_entries, GraphJsonStorage
from pytead.graph_utils import find_orphan_refs_in_rendered

def _scan_entries_for_orphans(entries_by_func: dict[str, list[dict]]):
    report = {}
    for fqn, entries in entries_by_func.items():
        orphans = []
        for e in entries:
            eg = e.get("result_graph")
            if eg is not None:
                orphans.extend(find_orphan_refs_in_rendered(eg))
        report[fqn] = {"entries": len(entries), "orphans": orphans}
    return report


def _scan_calls_dir_for_orphans(calls_dir: Path, formats=None):
    grouped = defaultdict(list)
    for e in iter_entries(Path(calls_dir), formats=formats):
        grouped[e["func"]].append(e)
    return _scan_entries_for_orphans(grouped)


def test_scan_entries_for_orphans_detects_simple_case(tmp_path: Path):
    entries = {
        "m.f": [
            {"func": "m.f", "args_graph": [], "kwargs_graph": {}, "result_graph": {"x": {"$ref": 9}}},
            {"func": "m.f", "args_graph": [], "kwargs_graph": {}, "result_graph": {"y": {"$id": 2}, "x": {"$ref": 2}}},
        ]
    }
    rep = _scan_entries_for_orphans(entries)
    assert rep["m.f"]["entries"] == 2
    assert ("$.x", 9) in rep["m.f"]["orphans"]


def test_scan_calls_dir_for_orphans_roundtrip(tmp_path: Path):
    # On écrit une trace graph-json minimale puis on scanne le dossier
    entry = {
        "func": "pkg.mod.fn",
        "args_graph": [],
        "kwargs_graph": {},
        "result_graph": {"a": {"$id": 1, "v": 0}, "b": {"$ref": 1}},
    }
    calls = tmp_path / "calls"
    calls.mkdir()
    st = GraphJsonStorage()
    p = st.make_path(calls, entry["func"])
    st.dump(entry, p)

    rep = _scan_calls_dir_for_orphans(calls, formats=["graph-json"])
    assert rep["pkg.mod.fn"]["entries"] == 1
    assert rep["pkg.mod.fn"]["orphans"] == []  # pas d’orphelin ici

