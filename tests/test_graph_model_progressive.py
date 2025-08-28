# -*- coding: utf-8 -*-
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


# ---------------------- 1) Fonction triviale (I/O scalaires) ----------------------

def inc(x: int) -> int:
    return x + 1

def test_graph_simple_function_scalar_io():
    out = inc(2)
    g = capture_object_graph(out)
    assert g == 3  # scalaires: capture = valeur brute


# ---------------------- 2) Méthode simple (ignorer self côté résultat) ----------------------

class Counter:
    def __init__(self, base: int = 10):
        self.base = base

    def add(self, x: int) -> int:
        return self.base + x

def test_graph_simple_method_result_only():
    c = Counter(7)
    out = c.add(5)
    g = capture_object_graph(out)
    assert g == 12


# ---------------------- 3) Méthode qui utilise des attributs ----------------------

class Accum:
    def __init__(self):
        self.total = 0
        self._private = 42  # doit être ignoré (clé "_" filtrée)

    def add(self, x: float) -> float:
        self.total += x
        return self.total

def test_graph_object_attributes_public_only():
    a = Accum()
    a.add(2.5)
    snap = capture_object_graph(a)
    assert snap == {"total": 2.5}  # uniquement les attributs publics


# ---------------------- 4) Objet peu complexe en entrée + sortie scalaire ----------------------

class Point:
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

def norm1(p: Point) -> float:
    return p.x + p.y

def test_graph_object_with_simple_attrs_as_input():
    p = Point(1.5, 2.5)
    gp = capture_object_graph(p)
    assert gp == {"x": 1.5, "y": 2.5}
    gout = capture_object_graph(norm1(p))
    assert gout == 4.0


# ---------------------- 5) Objet plus complexe (composition) ----------------------

class Segment:
    def __init__(self, a: Point, b: Point, name: str = "seg"):
        self.a = a
        self.b = b
        self.name = name

    def length_sq(self) -> float:
        dx = self.b.x - self.a.x
        dy = self.b.y - self.a.y
        return dx*dx + dy*dy

def test_graph_nested_object_and_scalar_result():
    p1 = Point(0.0, 0.0)
    p2 = Point(3.0, 4.0)
    s = Segment(p1, p2)
    gs = capture_object_graph(s)
    assert gs["name"] == "seg"
    assert gs["a"] == {"x": 0.0, "y": 0.0}
    assert gs["b"] == {"x": 3.0, "y": 4.0}
    assert capture_object_graph(s.length_sq()) == 25.0


# ---------------------- 6) Profondeur limitée ----------------------

class Box:
    def __init__(self, payload):
        self.payload = payload

def test_graph_max_depth_limits_recursion():
    nested = Box(Box(Point(1.0, 2.0)))
    g = capture_object_graph(nested, max_depth=2)
    # depth=2 => Box -> {payload: Box -> {payload: <repr/typename>}}
    assert isinstance(g, dict) and "payload" in g
    assert isinstance(g["payload"], dict) and "payload" in g["payload"]
    assert isinstance(g["payload"]["payload"], str)  # coupure


# ---------------------- 7) Conteneurs: tuple préservé, kwargs simples ----------------------

def f_mix(tup, *, k=0):
    # juste pour vérifier qu'on ne "listifie" pas les tuples côté capture
    return (tup, k)

def test_graph_preserves_tuple_and_kwargs():
    out = f_mix((1, 2), k=3)
    g = capture_object_graph(out)
    assert isinstance(g, tuple) and len(g) == 2
    assert g[0] == (1, 2)
    assert g[1] == 3


# ---------------------- 8) Références partagées (même objet vu 2x) ----------------------

def test_graph_shared_reference_is_encoded_as_ref_on_second_occurrence():
    shared = [1, 2]
    obj = {"u": shared, "v": shared}
    g = capture_object_graph(obj)
    # Première occurrence matérialisée, seconde en référence
    assert isinstance(g["u"], list)
    assert is_ref_dict(g["v"])
    assert isinstance(ref_value(g["v"]), int)


# ---------------------- 9) Cycle simple (auto-référence) ----------------------

def test_graph_cycle_list_encodes_reference_in_element():
    a = []
    a.append(a)  # cycle
    g = capture_object_graph(a, max_depth=5)
    # On attend: g est une liste, et le premier élément est un dict de référence
    assert isinstance(g, list) and len(g) == 1
    assert is_ref_dict(g[0])
    assert isinstance(ref_value(g[0]), int)


# ---------------------- 10) (Documentation) sets/frozensets — à améliorer ----------------------

def test_graph_sets_are_captured_canonically():
    s = {"b", "a"}
    g = capture_object_graph(s)
    # Attendu futur: représentation canonique ordonnée (ex: {"$set": ["a","b"]})
    assert g == {"$set": ["a", "b"], "$frozen": False}

