# -*- coding: utf-8 -*-
import json
from pathlib import Path

from pytead.tracing import trace
from pytead.storage import GraphJsonStorage

# --- Références: accepter l'ancien et le nouveau format ---
REF_KEYS = ("$$pytead_ref$$", "$ref")

def is_ref_dict(x) -> bool:
    return isinstance(x, dict) and any(k in x for k in REF_KEYS)

def ref_value(x):
    for k in REF_KEYS:
        if isinstance(x, dict) and k in x:
            return x[k]
    raise AssertionError(f"no ref key in {x!r}")


def test_tracing_graphjson_simple_function(tmp_path: Path):
    store = GraphJsonStorage()

    @trace(storage=store, storage_dir=tmp_path, limit=10)
    def double(x):
        return 2 * x

    assert double(21) == 42

    files = sorted(tmp_path.glob("*.gjson"))
    assert files, "aucune trace graph-json écrite"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["func"].endswith("double")
    assert data["args_graph"] == [21]
    assert data["kwargs_graph"] == {}
    assert data["result_graph"] == 42


def test_tracing_graphjson_instance_method_and_self_snapshot(tmp_path: Path):
    store = GraphJsonStorage()

    class Adder:
        def __init__(self, k: int):
            self.k = k
            self._hidden = "ignore me"

        @trace(storage=store, storage_dir=tmp_path, limit=10)
        def add(self, x: int) -> int:
            return self.k + x

    a = Adder(5)
    assert a.add(7) == 12

    files = sorted(tmp_path.glob("*.gjson"))
    assert files, "aucune trace graph-json écrite"
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    # args_graph[0] correspond au graphe de self ; seules les clés publiques doivent apparaître
    self_graph = data["args_graph"][0]
    assert isinstance(self_graph, dict)
    assert "k" in self_graph and self_graph["k"] == 5
    assert "_hidden" not in self_graph
    # le reste des champs
    assert data["args_graph"][1] == 7
    assert data["result_graph"] == 12

