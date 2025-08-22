# tests/test_tracing_self_snapshot.py
from __future__ import annotations

import glob
from pathlib import Path
from typing import Type

import pytest

from pytead.tracing import trace
from pytead.storage import PickleStorage, JsonStorage, ReprStorage


def _load_entries(dirpath: Path, storage) -> list[dict]:
    """Load all trace entries written in dirpath for the given storage backend."""
    out: list[dict] = []
    for p in sorted(dirpath.glob(f"*{storage.extension}")):
        try:
            out.append(storage.load(p))
        except Exception as exc:
            pytest.fail(f"Failed to load {p}: {exc}")
    return out


@pytest.mark.parametrize("StorageCls", [PickleStorage, JsonStorage, ReprStorage])
def test_function_tracing_basic(tmp_path: Path, StorageCls: Type):
    """Plain function: no 'self' snapshot; args/kwargs/result recorded; limit enforced."""
    storage = StorageCls()

    @trace(limit=3, storage_dir=tmp_path, storage=storage)
    def add(x, y=0):
        return x + y

    # Two root calls → two entries
    assert add(1, y=2) == 3
    assert add(10, y=-1) == 9

    entries = _load_entries(tmp_path, storage)
    assert len(entries) == 2
    for e in entries:
        assert "func" in e and e["func"].endswith(".add")
        assert "self" not in e
        assert isinstance(e.get("args"), tuple)
        assert isinstance(e.get("kwargs"), dict)
        assert e.get("timestamp", "").endswith("Z")

    # Values preserved
    xs = sorted((e["args"], e["kwargs"], e["result"]) for e in entries)
    assert xs[0] == ((1,), {"y": 2}, 3)
    assert xs[1] == ((10,), {"y": -1}, 9)


@pytest.mark.parametrize("StorageCls", [PickleStorage, JsonStorage, ReprStorage])
def test_instance_method_self_before_after(tmp_path: Path, StorageCls: Type):
    """
    Instance method: 'self' snapshot has public before/after AND full state_before/state_after.
    Private attributes are excluded from the public view but present in the full state.
    """
    storage = StorageCls()

    class Counter:
        def __init__(self):
            self.x = 0
            self._secret = 42  # private: should be excluded from public view

        @trace(limit=1, storage_dir=tmp_path, storage=storage)
        def inc(self, d=1):
            self.x += d
            return self.x

    c = Counter()
    assert c.inc(3) == 3

    entries = _load_entries(tmp_path, storage)
    assert len(entries) == 1
    e = entries[0]

    assert "self" in e and isinstance(e["self"], dict)
    s = e["self"]

    # Minimal keys always present
    assert {"type", "before", "after"}.issubset(s.keys())
    # Full state for rehydration also present
    assert "state_before" in s and "state_after" in s

    # Type string is fully qualified; allow locals — check suffix only
    assert isinstance(s["type"], str) and s["type"].endswith("Counter")

    # Public view excludes private fields
    assert s["before"].get("x") == 0
    assert "_secret" not in s["before"]
    assert s["after"].get("x") == 3
    assert "_secret" not in s["after"]

    # Full state includes private fields for rehydration
    assert s["state_before"].get("_secret") == 42
    assert s["state_after"].get("_secret") == 42


@pytest.mark.parametrize("StorageCls", [PickleStorage, JsonStorage, ReprStorage])
def test_static_and_class_methods_do_not_capture_self(tmp_path: Path, StorageCls: Type):
    """Static/class methods: no 'self' snapshot (heuristic relies on first param named 'self')."""
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

    entries = _load_entries(tmp_path, storage)
    assert len(entries) == 2
    # both entries should not have 'self'
    for e in entries:
        assert "self" not in e
        assert e["func"].endswith((".twice", ".name"))


@pytest.mark.parametrize("StorageCls", [PickleStorage, JsonStorage, ReprStorage])
def test_limit_is_respected(tmp_path: Path, StorageCls: Type):
    """When more calls than 'limit' occur, only 'limit' files are written."""
    storage = StorageCls()

    @trace(limit=2, storage_dir=tmp_path, storage=storage)
    def f(k):
        return k + 1

    for i in range(5):
        assert f(i) == i + 1

    entries = _load_entries(tmp_path, storage)
    assert len(entries) == 2  # capped by limit


@pytest.mark.parametrize("StorageCls", [PickleStorage, JsonStorage, ReprStorage])
def test_slots_are_snapshotted_and_callables_excluded(tmp_path: Path, StorageCls: Type):
    """__slots__ attributes are captured (non-private); callables are skipped."""
    storage = StorageCls()

    class S:
        __slots__ = ("a", "_hidden", "fn")

        def __init__(self):
            self.a = 1
            self._hidden = 99
            self.fn = lambda x: x  # callable → should be skipped

        @trace(limit=1, storage_dir=tmp_path, storage=storage)
        def bump(self, d=4):
            self.a += d
            # keep fn callable
            return self.a

    s = S()
    assert s.bump(8) == 9  # 1 + 8

    entries = _load_entries(tmp_path, storage)
    assert len(entries) == 1
    e = entries[0]
    assert "self" in e
    before = e["self"]["before"]
    after = e["self"]["after"]

    assert "a" in before and before["a"] == 1
    assert "a" in after and after["a"] == 9
    assert "_hidden" not in before and "_hidden" not in after
    # callable slot excluded
    assert "fn" not in before and "fn" not in after


@pytest.mark.parametrize("StorageCls", [JsonStorage, ReprStorage])
def test_json_and_repr_roundtrip_with_self(tmp_path: Path, StorageCls: Type):
    """
    For JSON/REPR storages, ensure the 'self' snapshot survives a dump/load
    and remains composed of literal-friendly values.
    """
    storage = StorageCls()

    class Box:
        def __init__(self, items):
            self.items = items  # list of ints → literal-friendly

        @trace(limit=1, storage_dir=tmp_path, storage=storage)
        def extend(self, more):
            self.items += list(more)
            return len(self.items)

    b = Box([1, 2])
    assert b.extend((3, 4)) == 4

    # Ensure at least one file of the right extension exists
    files = list(tmp_path.glob(f"*{storage.extension}"))
    assert files, f"No trace file with extension {storage.extension}"

    # Load back and check literal structure
    e = storage.load(files[0])
    assert isinstance(e["self"]["before"]["items"], (list, tuple))
    assert isinstance(e["self"]["after"]["items"], (list, tuple))
    assert e["self"]["before"]["items"] == [1, 2]
    assert e["self"]["after"]["items"] == [1, 2, 3, 4]
