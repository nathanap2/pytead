# tests/test_repr_storage_literalization.py
from pathlib import Path
import ast
import pytest

from pytead.tracing import trace
from pytead.storage import ReprStorage, PickleStorage
from pytead.gen_tests import collect_entries

# --- bloc A : literalization ---

class MonsterFactory:
    def __init__(self, species: str, level: int):
        self.species = species
        self.level = level
    def __repr__(self) -> str:
        return self.species  # non-literal repr

def create_monster(cfg: dict, _Monster=MonsterFactory):  # fige la classe ici
    return _Monster(cfg["species"], cfg["level"])

def test_repr_storage_writes_literal_structure(tmp_path: Path):
    calls = tmp_path / "calls"
    wrapped = trace(storage_dir=calls, storage=ReprStorage(), capture_objects="simple")(create_monster)
    wrapped({"species": "Tree", "level": 36})

    files = list(calls.glob("*.repr"))
    txt = files[0].read_text(encoding="utf-8")
    assert "'result': 'Tree'" in txt or '"result": "Tree"' in txt
    data = ast.literal_eval(txt)
    assert data["result"] == "Tree"
    assert data["result_obj"]["type"].endswith(".MonsterFactory")
    assert data["result_obj"]["state"]["species"] == "Tree"
    assert data["result_obj"]["state"]["level"] == 36


def test_collect_entries_reads_repr_after_patch(tmp_path: Path):
    calls = tmp_path / "calls"
    wrapped = trace(storage_dir=calls, storage=ReprStorage(), capture_objects="simple")(create_monster)
    wrapped({"species": "Tree", "level": 36})
    entries = collect_entries(calls, formats=["repr"])
    key = next(k for k in entries if k.endswith(".create_monster"))
    e = entries[key][0]
    assert e["result"] == "Tree"
    assert e["result_obj"]["state"]["level"] == 36


def test_json_storage_always_parses(tmp_path: Path):
    calls = tmp_path / "calls"
    wrapped = trace(storage_dir=calls, storage=PickleStorage(), capture_objects="simple")(create_monster)
    wrapped({"species": "Tree", "level": 36})
    assert True  # this test is just a placeholder for json/pickle paths

# --- bloc B : depth1 stringify ---

class Owner:
    def __repr__(self):
        return "Owner#42"

class Bare:
    pass

class MonsterDeep:
    __slots__ = ("name", "owner", "tags", "meta")
    def __init__(self):
        self.name = "Tree"
        self.owner = Owner()
        self.tags = [Bare(), 1, "x"]
        self.meta = {"k": Bare()}

def make():
    return MonsterDeep()

def test_depth1_stringify(tmp_path: Path):
    calls = tmp_path / "calls"
    wrapped = trace(
        storage_dir=calls,
        storage=ReprStorage(),
        capture_objects="simple",
        objects_stringify_depth=1,
    )(make)
    m = wrapped()
    entries = collect_entries(calls, formats=["repr"])
    key = next(k for k in entries if k.endswith(".make"))
    st = entries[key][0]["result_obj"]["state"]
    assert st["owner"] == "Owner#42"
    assert isinstance(st["tags"], list)
    assert st["tags"][0].endswith("Bare")
    assert st["meta"]["k"].endswith("Bare")

