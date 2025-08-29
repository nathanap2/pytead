import pytest
from pathlib import Path

from pytead.tracing import trace
from pytead.storage import PickleStorage, iter_entries


def count(dirpath: Path, pattern: str) -> int:
    return len(list(dirpath.glob(pattern)))


def test_root_call_only_with_recursion(tmp_path: Path):
    st = PickleStorage()

    @trace(limit=10, storage_dir=tmp_path, storage=st)
    def fact(n: int) -> int:
        return 1 if n <= 1 else n * fact(n - 1)

    assert fact(5) == 120

    # Une seule trace malgré la récursion.
    assert count(tmp_path, "*.pkl") == 1

    entries = list(iter_entries(tmp_path, formats=["pickle"]))
    assert len(entries) == 1
    e = entries[0]
    # Champs essentiels présents
    assert "trace_schema" in e and e["trace_schema"].startswith("pytead/")
    assert e["func"].endswith(".fact")
    assert e["args"] == (5,)
    assert e["kwargs"] == {}
    assert e["result"] == 120
    assert "timestamp" in e and isinstance(e["timestamp"], str)


def test_limit_is_respected(tmp_path: Path):
    st = PickleStorage()

    @trace(limit=2, storage_dir=tmp_path, storage=st)
    def inc(x: int) -> int:
        return x + 1

    for i in range(10):
        assert inc(i) == i + 1

    # Seulement 2 fichiers écrits
    assert count(tmp_path, "*.pkl") == 2

    entries = list(iter_entries(tmp_path, formats=["pickle"]))
    assert len(entries) == 2
    # Normalisations
    for e in entries:
        assert isinstance(e["args"], tuple)
        assert isinstance(e["kwargs"], dict)

@pytest.mark.xfail
def test_repr_storage_and_normalization_via_trace(tmp_path: Path):
    """Vérifie le chemin complet trace->dump(.repr)->iter_entries et les normalisations."""
    st = ReprStorage()

    @trace(limit=3, storage_dir=tmp_path, storage=st)
    def pair(a: int, b: int):
        return {"sum": a + b, "lst": [a, b]}

    pair(1, 2)
    pair(3, 4)

    # Fichiers .repr bien présents
    assert count(tmp_path, "*.repr") == 2

    # iter_entries normalise args -> tuple et kwargs -> dict
    entries = list(iter_entries(tmp_path, formats=["repr"]))
    assert len(entries) == 2
    for e in entries:
        assert isinstance(e["args"], tuple)
        assert e["kwargs"] == {}
        assert "sum" in e["result"]
        # la fonction renvoie déjà une liste ; on vérifie juste la structure attendue
        assert isinstance(e["result"]["lst"], list)

@pytest.mark.xfail
def test_repr_storage_preserves_tuples(tmp_path: Path):
    from pytead.storage import ReprStorage, iter_entries

    st = ReprStorage()

    entry = {
        "trace_schema": "pytead/v1",
        "func": "m.mod.f",
        "args": (1, (2, 3)),
        "kwargs": {"k": (4, 5)},
        "result": ({"a": (6,)}, [7, 8]),
        "timestamp": "2025-01-01T00:00:00Z",
    }
    path = tmp_path / "m_mod_f__x.repr"
    st.dump(entry, path)

    # lecture via iter_entries (normalisations incluses)
    entries = list(iter_entries(tmp_path, formats=["repr"]))
    assert len(entries) == 1
    e = entries[0]
    assert e["args"] == (1, (2, 3))
    assert e["kwargs"]["k"] == (4, 5)
    # tuple préservé dans le résultat
    assert isinstance(e["result"][0]["a"], tuple)


def test_all_storages_have_make_path():
    from pytead.storage import _REGISTRY

    for name, st in _REGISTRY.items():
        p = st.make_path(Path("."), "m.mod.f")  # ne doit pas lever
        assert p.suffix == st.extension
        assert isinstance(p, Path)

@pytest.mark.xfail
def test_repr_storage_preserves_int_keys(tmp_path: Path):
    from pytead.storage import ReprStorage, iter_entries

    st = ReprStorage()
    entry = {
        "trace_schema": "pytead/v1",
        "func": "m.mod.f",
        "args": (1,),
        "kwargs": {1: "a", (2, 3): "b"},
        "result": {10: "x", (4, 5): "y"},
        "timestamp": "2025-01-01T00:00:00Z",
    }
    p = st.make_path(tmp_path, entry["func"])
    st.dump(entry, p)

    e = list(iter_entries(tmp_path, formats=["repr"]))[0]
    # Clés int et tuple préservées
    assert 1 in e["kwargs"] and e["kwargs"][1] == "a"
    assert (2, 3) in e["kwargs"] and e["kwargs"][(2, 3)] == "b"
    assert 10 in e["result"] and e["result"][10] == "x"
    assert (4, 5) in e["result"] and e["result"][(4, 5)] == "y"
