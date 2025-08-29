# tests/test_capture_strict_monkeypatch.py
from __future__ import annotations

import pytest
import pytead.graph_capture as gc
from pytead.graph_utils import find_orphan_refs_in_rendered
from pytead.errors import GraphCaptureRefToUnanchored


def _strict_wrapper(orig):
    """
    Non-reentrant wrapper: validates only at the *top level*.

    Rationale:
      - We want to check that `capture_object_graph` produces a rendered graph
        without orphan `{"$ref": N}`.
      - Recursive calls inside the capture logic should bypass the check so we
        do not validate partial subgraphs.

    Behavior:
      - On first-level invocation, run `orig`, then scan the resulting
        rendered graph with `find_orphan_refs_in_rendered`.
      - If any orphan refs are found, raise `GraphCaptureRefToUnanchored`.
    """
    in_progress = False

    def _wrapped(obj, **kw):
        nonlocal in_progress
        if in_progress:
            # Recursive call: skip the strict check on partial subgraphs
            return orig(obj, **kw)
        in_progress = True
        try:
            g = orig(obj, **kw)
        finally:
            in_progress = False

        orphans = find_orphan_refs_in_rendered(g)
        if orphans:
            details = ", ".join(f"{p} -> ref={rid}" for p, rid in orphans)
            raise GraphCaptureRefToUnanchored(f"Orphan refs in rendered capture: {details}")
        return g

    return _wrapped


def test_strict_capture_raises_on_aliasing_tuple(monkeypatch):
    orig = gc.capture_object_graph
    monkeypatch.setattr(gc, "capture_object_graph", _strict_wrapper(orig))

    shared = (1, 2)
    with pytest.raises(GraphCaptureRefToUnanchored):
        gc.capture_object_graph({"x": [shared, shared]}, max_depth=5)


def test_strict_capture_ok_without_aliasing(monkeypatch):
    orig = gc.capture_object_graph
    monkeypatch.setattr(gc, "capture_object_graph", _strict_wrapper(orig))

    # Two *distinct* tuples (avoid constant re-use/aliasing)
    t1 = tuple([1, 2])
    t2 = tuple([1, 2])

    g = gc.capture_object_graph({"x": [t1, t2]}, max_depth=5)
    assert isinstance(g, dict)

