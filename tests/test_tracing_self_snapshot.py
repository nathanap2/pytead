# tests/test_tracing_self_snapshot.py
from __future__ import annotations

from pathlib import Path
from typing import Type
import pytest

from pytead.tracing import trace
from pytead.storage import PickleStorage, ReprStorage, iter_entries


# --- Helpers ---

FMT_BY_EXT = {
    ".pkl": "pickle",
    ".json": "json",
    ".repr": "repr",
}


def _format_for_storage(storage) -> str:
    return FMT_BY_EXT[storage.extension]


def _load_entries_normed(dirpath: Path, storage) -> list[dict]:
    """Lecture normalisée via iter_entries pour rendre les assertions stables."""
    return list(iter_entries(dirpath, formats=[_format_for_storage(storage)]))


@pytest.mark.parametrize("StorageCls", [PickleStorage, ReprStorage])
def test_function_tracing_basic(tmp_path: Path, StorageCls: Type):
    """Plain function: no 'self' snapshot; args/kwargs/result recorded; limit enforced."""
    storage = StorageCls()

    @trace(limit=3, storage_dir=tmp_path, storage=storage)
    def add(x, y=0):
        return x + y

    assert add(1, y=2) == 3
    assert add(10, y=-1) == 9

    entries = _load_entries_normed(tmp_path, storage)
    assert len(entries) == 2

    for e in entries:
        assert "func" in e and e["func"].endswith(".add")
        assert "self" not in e
        assert isinstance(e.get("args"), tuple)
        assert isinstance(e.get("kwargs"), dict)
        assert e.get("timestamp", "").endswith("Z")

    # Comparaison robuste: pas de tri sur des dicts, on compare un set de triplets normalisés
    got = {(e["args"], e["kwargs"].get("y"), e["result"]) for e in entries}
    assert got == {((1,), 2, 3), ((10,), -1, 9)}


@pytest.mark.parametrize("StorageCls", [PickleStorage, ReprStorage])
def test_instance_method_self_before_after(tmp_path: Path, StorageCls: Type):
    storage = StorageCls()

    class Counter:
        def __init__(self):
            self.x = 0
            self._secret = 42  # private

        @trace(limit=1, storage_dir=tmp_path, storage=storage)
        def inc(self, d=1):
            self.x += d
            return self.x

    c = Counter()
    assert c.inc(3) == 3

    entries = _load_entries_normed(tmp_path, storage)
    assert len(entries) == 1
    e = entries[0]

    assert "self" in e and isinstance(e["self"], dict)
    s = e["self"]

    assert {"type", "before", "after"}.issubset(s.keys())
    assert "state_before" in s and "state_after" in s

    assert isinstance(s["type"], str) and s["type"].endswith("Counter")

    assert s["before"].get("x") == 0
    assert "_secret" not in s["before"]
    assert s["after"].get("x") == 3
    assert "_secret" not in s["after"]

    assert s["state_before"].get("_secret") == 42
    assert s["state_after"].get("_secret") == 42


@pytest.mark.parametrize("StorageCls", [PickleStorage, ReprStorage])
def test_static_and_class_methods_do_not_capture_self(tmp_path: Path, StorageCls: Type):
    storage = StorageCls()

    class Util:
        @trace(limit=2, storage_dir=tmp_path, storage=storage)
        @staticmethod
        def twice(x):
            return 2 * x

        @trace(limit=2, storage_dir=tmp_path, storage=storage)
        @classmethod
        def name(cls):
            return cls.__name__

    assert Util.twice(5) == 10
    assert Util.name().endswith("Util")

    entries = _load_entries_normed(tmp_path, storage)
    assert len(entries) == 2
    for e in entries:
        assert "self" not in e
        assert e["func"].endswith((".twice", ".name"))


@pytest.mark.parametrize("StorageCls", [PickleStorage, ReprStorage])
def test_limit_is_respected(tmp_path: Path, StorageCls: Type):
    storage = StorageCls()

    @trace(limit=2, storage_dir=tmp_path, storage=storage)
    def f(k):
        return k + 1

    for i in range(5):
        assert f(i) == i + 1

    entries = _load_entries_normed(tmp_path, storage)
    assert len(entries) == 2  # capped by limit


@pytest.mark.parametrize("StorageCls", [PickleStorage, ReprStorage])
def test_slots_are_snapshotted_and_callables_excluded(tmp_path: Path, StorageCls: Type):
    storage = StorageCls()

    class S:
        __slots__ = ("a", "_hidden", "fn")

        def __init__(self):
            self.a = 1
            self._hidden = 99
            self.fn = lambda x: x  # callable

        @trace(limit=1, storage_dir=tmp_path, storage=storage)
        def bump(self, d=4):
            self.a += d
            return self.a

    s = S()
    assert s.bump(8) == 9

    entries = _load_entries_normed(tmp_path, storage)
    assert len(entries) == 1
    e = entries[0]
    assert "self" in e
    before = e["self"]["before"]
    after = e["self"]["after"]

    assert "a" in before and before["a"] == 1
    assert "a" in after and after["a"] == 9
    assert "_hidden" not in before and "_hidden" not in after
    assert "fn" not in before and "fn" not in after


def test_repr_roundtrip_with_self(tmp_path):
    """
    Vérifie la structure *brute* écrite/relue par le backend REPR via storage.load(...).
    """
    from pytead.tracing import trace
    from pytead.storage import ReprStorage

    storage = ReprStorage()

    class Box:
        def __init__(self, items):
            self.items = items

        @trace(limit=1, storage_dir=tmp_path, storage=storage)
        def extend(self, more):
            self.items += list(more)
            return len(self.items)

    b = Box([1, 2])
    assert b.extend((3, 4)) == 4

    files = list(tmp_path.glob(f"*{storage.extension}"))
    assert files, f"No trace file with extension {storage.extension}"

    e = storage.load(files[0])
    # Les états 'before/after' existent et contiennent 'items' sous forme de séquence
    assert isinstance(e["self"]["before"]["items"], (list, tuple))
    assert isinstance(e["self"]["after"]["items"], (list, tuple))
    assert e["self"]["before"]["items"] == [1, 2]
    assert e["self"]["after"]["items"] == [1, 2, 3, 4]

