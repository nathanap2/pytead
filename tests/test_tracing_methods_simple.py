# tests/test_tracing_methods_simple.py
from pathlib import Path
import inspect

from pytead.tracing import trace
from pytead.storage import iter_entries


import inspect
from pathlib import Path

from pytead.tracing import trace
from pytead.storage import GraphJsonStorage, iter_entries

def test_tracing_methods_decorator_like_wrapping__graphjson(tmp_path: Path):
    """
    Phase 1: ensure method traces carry module.Class.method via __qualname__,
    and that storage works identically for instance/static/class methods.
    """
    st = GraphJsonStorage()

    class Calc:
        @staticmethod
        def smul(a, b):
            return a * b

        @classmethod
        def tag(cls, s):
            return f"{cls.__name__}:{s}"

        def add(self, a, b):
            return a + b

    # --- Emulate CLI wrapping (descriptor-aware) ---

    # Instance method: attribute on the class is a plain function → wrap and rebind
    Calc.add = trace(limit=10, storage_dir=tmp_path, storage=st)(Calc.add)

    # Static method: wrap underlying __func__, then reattach as staticmethod
    sm_raw = inspect.getattr_static(Calc, "smul")
    assert isinstance(sm_raw, staticmethod)
    Calc.smul = staticmethod(
        trace(limit=10, storage_dir=tmp_path, storage=st)(sm_raw.__func__)
    )

    # Class method: wrap underlying __func__, then reattach as classmethod
    cm_raw = inspect.getattr_static(Calc, "tag")
    assert isinstance(cm_raw, classmethod)
    Calc.tag = classmethod(
        trace(limit=10, storage_dir=tmp_path, storage=st)(cm_raw.__func__)
    )

    # --- Exercise calls ---
    assert Calc().add(2, 3) == 5
    assert Calc.smul(4, 5) == 20
    assert Calc.tag("X") == "Calc:X"

    # --- Read back entries ---
    entries = list(iter_entries(tmp_path, formats=["graph-json"]))
    names = sorted(e["func"] for e in entries)

    # Qualnames should include the class
    assert any(n.endswith(".Calc.add") for n in names)
    assert any(n.endswith(".Calc.smul") for n in names)
    assert any(n.endswith(".Calc.tag") for n in names)

    by = {e["func"]: e for e in entries}

    # Instance method: args_graph = [self_graph, 2, 3], result_graph = 5
    e_add = by[next(k for k in by if k.endswith(".Calc.add"))]
    ag = e_add.get("args_graph", [])
    assert e_add["result_graph"] == 5
    assert isinstance(ag, list) and len(ag) >= 3
    assert isinstance(ag[0], dict)      # self capturé comme graphe (porte en général $id/attributs)
    assert ag[1:] == [2, 3]

    # Staticmethod: args_graph = [4, 5], result_graph = 20 (pas de self/cls)
    e_smul = by[next(k for k in by if k.endswith(".Calc.smul"))]
    assert e_smul.get("args_graph") == [4, 5]
    assert e_smul.get("result_graph") == 20

    # Classmethod: args_graph = [cls_graph, "X"], result_graph = "Calc:X"
    e_tag = by[next(k for k in by if k.endswith(".Calc.tag"))]
    ag_tag = e_tag.get("args_graph", [])
    assert isinstance(ag_tag, list) and len(ag_tag) == 2
    assert isinstance(ag_tag[0], dict)   # la classe est capturée comme graphe
    assert ag_tag[1] == "X"
    assert e_tag.get("result_graph") == "Calc:X"

