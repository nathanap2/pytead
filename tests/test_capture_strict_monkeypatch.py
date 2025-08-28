# tests/test_capture_strict_monkeypatch.py
from __future__ import annotations
import pytest
import pytead.graph_capture as gc
from pytead.graph_utils import find_orphan_refs
from pytead.errors import GraphCaptureRefToUnanchored

from pytead.graph_utils import find_orphan_refs
from pytead.errors import GraphCaptureRefToUnanchored

def _strict_wrapper(orig):
    """
    Enveloppe non-réentrante : ne vérifie qu'au niveau *top-level*.
    Les appels récursifs utilisent `orig` directement pour éviter
    de traiter des sous-graphes partiels.
    """
    in_progress = False

    def _wrapped(obj, **kw):
        nonlocal in_progress
        if in_progress:
            # sous-appel : pas de check strict ici
            return orig(obj, **kw)
        in_progress = True
        try:
            g = orig(obj, **kw)
        finally:
            in_progress = False

        orphans = find_orphan_refs(g)
        if orphans:
            details = ", ".join(f"{p} -> ref={rid}" for p, rid in orphans)
            raise GraphCaptureRefToUnanchored(f"Orphan refs in capture: {details}")
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

    # Deux tuples *distincts* (évite la réutilisation de constantes)
    t1 = tuple([1, 2])
    t2 = tuple([1, 2])

    g = gc.capture_object_graph({"x": [t1, t2]}, max_depth=5)
    assert isinstance(g, dict)


