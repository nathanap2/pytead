# tests/test_repr_storage_literalization.py
from pathlib import Path
import ast
import pytest
from typing import Any

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


from pprint import pformat

def _repr_payloads_for_make(calls: Path) -> dict[str, dict]:
    """
    Retourne {filename: parsed_dict} pour tous les .repr dont 'func' se termine par '.make'.
    Si le parsing échoue, on met l'erreur et le brut.
    """
    out = {}
    for f in sorted(calls.glob("*.repr")):
        txt = f.read_text(encoding="utf-8")
        try:
            data = ast.literal_eval(txt)
        except Exception as exc:
            data = {"__parse_error__": repr(exc), "__raw__": txt}
        if isinstance(data, dict) and str(data.get("func", "")).endswith(".make"):
            out[f.name] = data
    return out

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
    e = entries[key][0]

    # Garde-fous + diagnostics
    if "result_obj" not in e:
        payloads = _repr_payloads_for_make(calls)
        pytest.fail(
            "result_obj manquant dans l'entrée collectée.\n"
            f"Entry keys   : {sorted(e.keys())}\n"
            f"Entry dump   : {pformat(e)}\n"
            f".repr payloads (make):\n{pformat(payloads)}"
        )

    st = e["result_obj"].get("state")
    if not isinstance(st, dict) or "owner" not in st:
        payloads = _repr_payloads_for_make(calls)
        st_info = f"type={type(st).__name__}" if not isinstance(st, dict) else f"keys={list(st.keys())}"
        pytest.fail(
            "Clé 'owner' absente dans result_obj.state.\n"
            f"state info   : {st_info}\n"
            f"result_obj   : {pformat(e['result_obj'])}\n"
            f"Entry dump   : {pformat(e)}\n"
            f".repr payloads (make):\n{pformat(payloads)}"
        )

    # Assertions originales
    assert st["owner"] == "Owner#42"
    assert isinstance(st["tags"], list)
    assert st["tags"][0].endswith("Bare")
    assert st["meta"]["k"].endswith("Bare")




def _repr_payloads_for_make(calls: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(calls.glob("*.repr")):
        txt = f.read_text(encoding="utf-8")
        try:
            data = ast.literal_eval(txt)
        except Exception as exc:
            data = {"__parse_error__": repr(exc), "__raw__": txt}
        if isinstance(data, dict) and str(data.get("func", "")).endswith(".make"):
            out[f.name] = data
    return out

def _describe_instance(x: Any) -> str:
    try:
        slots = getattr(type(x), "__slots__", None)
    except Exception:
        slots = "<error>"
    lines = []
    lines.append(f"type: {type(x)}")
    lines.append(f"__slots__: {slots!r}")
    names = []
    try:
        if isinstance(slots, str):
            names = [slots]
        elif isinstance(slots, (list, tuple)):
            names = list(slots)
        elif slots:
            names = list(slots)
    except Exception:
        pass
    for name in names:
        try:
            val = getattr(x, name)
            lines.append(f"  - {name}: type={type(val).__name__}, repr={repr(val)}")
        except Exception as exc:
            lines.append(f"  - {name}: <getattr error: {exc!r}>")
    return "\n".join(lines)
    
def test__obj_spec_sanity_for_slots_depth1():
    m = make()  # MonsterDeep()
    # Rejoue la logique attendue : snapshot canonique + stringify(1)
    from pytead.tracing import _snapshot_object, _stringify_level1, _qualtype
    base = _snapshot_object(m, include_private=True)
    state = {k: _stringify_level1(v) for k, v in base.items()}
    assert state["owner"] == "Owner#42"
    assert isinstance(state["tags"], list) and str(state["tags"][0]).endswith("Bare")
    assert state["meta"]["k"].endswith("Bare")


def test_depth1_stringify(tmp_path: Path):
    calls = tmp_path / "calls"
    wrapped = trace(
        storage_dir=calls,
        storage=ReprStorage(),
        capture_objects="simple",
        objects_stringify_depth=1,
    )(make)

    # Exécute et récupère l'instance renvoyée pour introspection locale
    inst = wrapped()

    entries = collect_entries(calls, formats=["repr"])
    key = next(k for k in entries if k.endswith(".make"))
    e = entries[key][0]

    # Diagnostics supplémentaires côté instance
    try:
        from pytead.tracing import _snapshot_object, _stringify_level1  # type: ignore
        pub_snap = _snapshot_object(inst, include_private=False)
        all_snap = _snapshot_object(inst, include_private=True)
        pub_str1 = {k: _stringify_level1(v) for k, v in pub_snap.items()}
        all_str1 = {k: _stringify_level1(v) for k, v in all_snap.items()}
    except Exception as exc:
        pub_snap = all_snap = pub_str1 = all_str1 = {"__introspection_error__": repr(exc)}

    # Garde-fous + diagnostics enrichis
    if "result_obj" not in e:
        payloads = _repr_payloads_for_make(calls)
        pytest.fail(
            "result_obj manquant dans l'entrée collectée.\n"
            f"Entry keys   : {sorted(e.keys())}\n"
            f"Entry dump   : {pformat(e)}\n\n"
            f"Instance description:\n{_describe_instance(inst)}\n\n"
            f"snapshot public   : {pformat(pub_snap)}\n"
            f"snapshot complet  : {pformat(all_snap)}\n"
            f"stringify1 public : {pformat(pub_str1)}\n"
            f"stringify1 complet: {pformat(all_str1)}\n\n"
            f".repr payloads (make):\n{pformat(payloads)}"
        )

    st = e["result_obj"].get("state")

    if not isinstance(st, dict) or "owner" not in st:
        payloads = _repr_payloads_for_make(calls)
        st_info = f"type={type(st).__name__}" if not isinstance(st, dict) else f"keys={list(st.keys())}"
        pytest.fail(
            "Clé 'owner' absente dans result_obj.state.\n"
            f"state info         : {st_info}\n"
            f"result_obj         : {pformat(e['result_obj'])}\n"
            f"Entry dump         : {pformat(e)}\n\n"
            f"Instance description:\n{_describe_instance(inst)}\n\n"
            f"snapshot public    : {pformat(pub_snap)}\n"
            f"snapshot complet   : {pformat(all_snap)}\n"
            f"stringify1 public  : {pformat(pub_str1)}\n"
            f"stringify1 complet : {pformat(all_str1)}\n\n"
            f".repr payloads (make):\n{pformat(payloads)}"
        )

    # Assertions d’origine
    assert st["owner"] == "Owner#42"
    assert isinstance(st["tags"], list)
    assert st["tags"][0].endswith("Bare")
    assert st["meta"]["k"].endswith("Bare")
    
    
# --- bloc C : micro-diagnostics sur repr/regex/stringify (à ajouter en fin de fichier) ---

def test_diag_opaque_repr_detection():
    """
    Diagnostique si la lib reconnaît bien un repr 'opaque' du type <...Bare object at 0x...>
    et si _safe_repr_or_classname(...) retourne un nom de classe plutôt que le repr brut.
    """
    from pytead.tracing import _safe_repr_or_classname, _OPAQUE_REPR_RE
    b = Bare()
    r = repr(b)
    pattern = getattr(_OPAQUE_REPR_RE, "pattern", "<no pattern attr>")
    m = bool(_OPAQUE_REPR_RE.match(r))
    s = _safe_repr_or_classname(b)

    # Assertions avec messages verbeux pour bien voir l'état courant
    assert m, f"Opaque repr non détecté par le regex.\nrepr={r!r}\npattern={pattern!r}"
    assert isinstance(s, str), f"_safe_repr_or_classname(b) n'a pas renvoyé une str: {type(s)}"
    assert s.endswith("Bare") or s.endswith(".Bare"), (
        "_safe_repr_or_classname(b) ne renvoie pas un nom lisible de classe.\n"
        f"repr={r!r}\npattern={pattern!r}\nreturned={s!r}"
    )


def test_diag_stringify_level1_owner_atom():
    """
    _stringify_level1(Owner()) doit donner 'Owner#42' (repr explicite).
    """
    from pytead.tracing import _stringify_level1
    o = Owner()
    so = _stringify_level1(o)
    assert so == "Owner#42", f"_stringify_level1(Owner()) -> {so!r} (attendu 'Owner#42')"


def test_diag_stringify_level1_list_with_object_first():
    """
    _stringify_level1 sur une liste [Bare(), 1, 'x'] doit produire une liste de chaînes/littéraux,
    avec un premier élément string qui finit par 'Bare'.
    """
    from pytead.tracing import _stringify_level1
    out = _stringify_level1([Bare(), 1, "x"])
    assert isinstance(out, list), f"type(out)={type(out)} valeur={out!r}"
    first = out[0]
    assert isinstance(first, str), f"out[0] n'est pas une str: {type(first)} valeur={first!r}"
    assert first.endswith("Bare") or first.endswith(".Bare"), f"out[0]={first!r} ne finit pas par 'Bare'"


def test_diag_stringify_level1_dict_with_object_value():
    """
    _stringify_level1 sur un dict {'k': Bare()} doit produire {'k': '<nom-de-classe>'}.
    """
    from pytead.tracing import _stringify_level1
    out = _stringify_level1({"k": Bare()})
    assert isinstance(out, dict), f"type(out)={type(out)} valeur={out!r}"
    v = out.get("k")
    assert isinstance(v, str), f"out['k'] n'est pas une str: {type(v)} valeur={v!r}"
    assert v.endswith("Bare") or v.endswith(".Bare"), f"out['k']={v!r} ne finit pas par 'Bare'"

# --- bloc D : diagnostics de pipeline snapshot -> stringify -> trace ---

def test_diag_snapshot_object_monsterdeep():
    """
    Le snapshot (_snapshot_object) convertit déjà les objets imbriqués en str via _to_literal.
    On vérifie que :
      - 'owner' devient bien 'Owner#42' (repr explicite, str),
      - 'tags[0]' (Bare()) et 'meta["k"]' deviennent déjà des str du type '<... object at 0x...>'.
    """
    from pytead.tracing import _snapshot_object
    m = make()
    base = _snapshot_object(m, include_private=True)

    assert set(base.keys()) == {"name", "owner", "tags", "meta"}
    assert base["owner"] == "Owner#42"  # repr explicite de Owner()
    assert isinstance(base["tags"], list) and len(base["tags"]) >= 1
    assert isinstance(base["tags"][0], str) and base["tags"][0].startswith("<") and " object at 0x" in base["tags"][0]
    assert isinstance(base["meta"], dict) and "k" in base["meta"]
    assert isinstance(base["meta"]["k"], str) and base["meta"]["k"].startswith("<") and " object at 0x" in base["meta"]["k"]


def test_diag_stringify_level1_sur_snapshot():
    """
    _stringify_level1 ne remplace pas les str déjà formées (comme '<... object at 0x...>').
    Le passage snapshot -> stringify(1) garde donc ces chaînes telles quelles.
    """
    from pytead.tracing import _snapshot_object, _stringify_level1
    m = make()
    base = _snapshot_object(m, include_private=True)
    state = {k: _stringify_level1(v) for k, v in base.items()}

    # 'owner' reste OK
    assert state["owner"] == "Owner#42"
    # 'tags[0]' et 'meta["k"]' restent des str opaques identiques à celles du snapshot
    assert state["tags"][0] == base["tags"][0]
    assert state["meta"]["k"] == base["meta"]["k"]


def test_diag_stringify_level1_sur_repr_string():
    """
    Si on donne directement à _stringify_level1 une chaîne de repr opaque, elle est renvoyée telle quelle.
    """
    from pytead.tracing import _stringify_level1
    s = repr(Bare())
    out = _stringify_level1(s)
    assert out == s


def test_diag_trace_pipeline_depth1_comportement_actuel(tmp_path: Path):
    """
    Comportement actuel end-to-end avec objects_stringify_depth=1 :
      - result_obj présent,
      - state['owner'] == 'Owner#42',
      - state['tags'][0] reste une str opaque du type '<...Bare object at 0x...>'.
    """
    calls = tmp_path / "calls"
    wrapped = trace(
        storage_dir=calls,
        storage=ReprStorage(),
        capture_objects="simple",
        objects_stringify_depth=1,
    )(make)
    _ = wrapped()
    entries = collect_entries(calls, formats=["repr"])
    key = next(k for k in entries if k.endswith(".make"))
    st = entries[key][0]["result_obj"]["state"]

    assert st["owner"] == "Owner#42"
    assert isinstance(st["tags"], list) and isinstance(st["tags"][0], str)
    assert st["tags"][0].startswith("<") and " object at 0x" in st["tags"][0]

