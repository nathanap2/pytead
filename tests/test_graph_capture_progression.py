# -*- coding: utf-8 -*-
import math
import pytest

from pytead.graph_capture import capture_object_graph

# --- Références: accepter l'ancien et le nouveau format ---
REF_KEYS = ("$$pytead_ref$$", "$ref")

def is_ref_dict(x) -> bool:
    return isinstance(x, dict) and any(k in x for k in REF_KEYS)

def ref_value(x):
    for k in REF_KEYS:
        if isinstance(x, dict) and k in x:
            return x[k]
    raise AssertionError(f"no ref key in {x!r}")


# ---------- 1) Fonction simple: entrées et sortie scalaires ----------

def inc(x: int) -> int:
    return x + 1

def test_graph_simple_function_scalar_io():
    out = inc(2)
    g = capture_object_graph(out)
    assert g == 3  # scalaires inchangés


# ---------- 2) Méthode simple: ignorer "self" (on teste seulement le résultat) ----------

class Counter:
    def __init__(self, base: int = 10) -> None:
        self.base = base

    def add(self, x: int) -> int:
        return self.base + x

def test_graph_simple_method_result_only():
    c = Counter(7)
    out = c.add(5)
    g = capture_object_graph(out)
    assert g == 12


# ---------- 3) Méthodes qui utilisent des attributs ----------

class Accum:
    def __init__(self) -> None:
        self.total = 0
        self._private = 42  # doit être ignoré par capture (clé "_" filtrée)

    def add(self, x: float) -> float:
        self.total += x
        return self.total

def test_graph_object_attributes_public_only():
    a = Accum()
    a.add(2.5)
    snap = capture_object_graph(a)
    # Seuls les attributs publics apparaissent
    assert "total" in snap and snap["total"] == 2.5
    assert "_private" not in snap


# ---------- 4) Objet "peu complexe" en entrée ----------

class Point:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y

def norm1(p: Point) -> float:
    return p.x + p.y

def test_graph_object_with_simple_attrs_as_input():
    p = Point(1.5, 2.5)
    gp = capture_object_graph(p)
    assert gp == {"x": 1.5, "y": 2.5}

    out = norm1(p)
    gout = capture_object_graph(out)
    assert gout == 4.0


# ---------- 5) Objet plus complexe (composition) ----------

class Segment:
    def __init__(self, a: Point, b: Point, name: str = "seg") -> None:
        self.a = a
        self.b = b
        self.name = name

    def length_sq(self) -> float:
        dx = self.b.x - self.a.x
        dy = self.b.y - self.a.y
        return dx*dx + dy*dy

def test_graph_nested_object():
    p1 = Point(0.0, 0.0)
    p2 = Point(3.0, 4.0)
    s = Segment(p1, p2)
    gs = capture_object_graph(s)
    # graphe imbriqué
    assert gs["name"] == "seg"
    assert gs["a"] == {"x": 0.0, "y": 0.0}
    assert gs["b"] == {"x": 3.0, "y": 4.0}

    out = s.length_sq()
    gout = capture_object_graph(out)
    assert gout == 25.0


# ---------- 6) Profondeur limitée ----------

class Box:
    def __init__(self, payload):
        self.payload = payload

def test_graph_max_depth_limits_recursion():
    nested = Box(Box(Point(1.0, 2.0)))
    g1 = capture_object_graph(nested, max_depth=2)
    # à depth=2: Box -> {payload: Box -> {payload: <repr/classname>}}
    assert isinstance(g1, dict) and "payload" in g1
    assert isinstance(g1["payload"], dict) and "payload" in g1["payload"]
    assert isinstance(g1["payload"]["payload"], str)  # coupure par repr/typename


# ---------- 7) FUTUR: références partagées / cycles (marqué xfail tant que pas de labels stables) ----------

def test_graph_shared_references_are_stable_across_runs():
    shared = [1, 2]
    obj = {"u": shared, "v": shared}

    # Deux captures indépendantes (simule "runs" successifs)
    g1 = capture_object_graph(obj)
    g2 = capture_object_graph(obj)

    # Première occurrence matérialisée, seconde encodée en référence
    assert isinstance(g1["u"], list)
    assert is_ref_dict(g1["v"])
    assert isinstance(g2["u"], list)
    assert is_ref_dict(g2["v"])

    # Le label de référence doit être identique d'une capture à l'autre
    assert isinstance(ref_value(g1["v"]), int)
    assert ref_value(g1["v"]) == ref_value(g2["v"])


def test_graph_cycle_does_not_infinite_loop_and_is_stable():
    a = []
    a.append(a)  # cycle
    g = capture_object_graph(a, max_depth=5)
    # Au minimum : pas de boucle infinie, et la structure encode une auto-référence.
    assert isinstance(g, list) or isinstance(g, tuple)
    # Invariante “faible” : la représentation contient une marque de ref (selon format)
    assert "ref" in repr(g).lower()

