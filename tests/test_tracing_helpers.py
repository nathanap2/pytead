# tests/test_tracing_helpers.py
from pytead.tracing import _is_builtin_like, _obj_spec

def test_is_builtin_like_simple():
    assert _is_builtin_like(3)
    assert _is_builtin_like([1,2])
    class C: pass
    assert not _is_builtin_like(C())

def test_obj_spec_depth0(tmp_path):
    class C:
        def __init__(self): self.x = 1
    s = _obj_spec(C(), include_private=False, stringify_depth=0)
    assert s and s["type"].endswith(".C") and "x" in s["state"]

def test_obj_spec_depth1_handles_nested():
    class D:
        def __init__(self): self.t = (object(), {"k": object()})
    s = _obj_spec(D(), include_private=True, stringify_depth=1)
    assert s and "t" in s["state"]

