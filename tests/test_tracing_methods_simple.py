# tests/test_tracing_methods_simple.py
from pathlib import Path
import inspect

from pytead.tracing import trace
from pytead.storage import JsonStorage, iter_entries


def test_tracing_methods_decorator_like_wrapping(tmp_path: Path):
    """
    Phase 1: ensure method traces carry module.Class.method via __qualname__,
    and that storage works identically for instance/static/class methods.

    We mimic the CLI's descriptor-aware wrapping at definition site:
    - instance method: wrap function and rebind on the class
    - staticmethod/classmethod: wrap underlying __func__, then rewrap

    NOTE: use JsonStorage because local classes/instances aren't picklable.
    """
    st = JsonStorage()

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

    # Static method: get the descriptor, wrap __func__, then reattach as staticmethod
    sm_raw = inspect.getattr_static(Calc, "smul")
    assert isinstance(sm_raw, staticmethod)
    Calc.smul = staticmethod(
        trace(limit=10, storage_dir=tmp_path, storage=st)(sm_raw.__func__)
    )

    # Class method: get the descriptor, wrap __func__, then reattach as classmethod
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
    entries = list(iter_entries(tmp_path, formats=["json"]))
    names = sorted(e["func"] for e in entries)

    # Qualnames should include the class
    assert any(n.endswith(".Calc.add") for n in names)
    assert any(n.endswith(".Calc.smul") for n in names)
    assert any(n.endswith(".Calc.tag") for n in names)

    # Sanity on args/results for each (JSON uses repr for non-JSONable objects)
    by = {e["func"]: e for e in entries}

    # add(self, 2, 3) — first arg is repr(self) as a str; tail are the numbers
    k_add = next(k for k in by if k.endswith(".Calc.add"))
    assert by[k_add]["result"] == 5
    assert isinstance(by[k_add]["args"][0], str) and "Calc" in by[k_add]["args"][0]
    assert by[k_add]["args"][1:] == (2, 3)

    # staticmethod: plain args
    k_smul = next(k for k in by if k.endswith(".Calc.smul"))
    assert by[k_smul]["args"] == (4, 5) and by[k_smul]["result"] == 20

    # classmethod: first arg is repr(cls) as a str; tail is ("X",)
    k_tag = next(k for k in by if k.endswith(".Calc.tag"))
    assert isinstance(by[k_tag]["args"][0], str) and "Calc" in by[k_tag]["args"][0]
    assert by[k_tag]["args"][1:] == ("X",)
    assert by[k_tag]["result"] == "Calc:X"

