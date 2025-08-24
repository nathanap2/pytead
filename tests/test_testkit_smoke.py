# tests/test_testkit_smoke.py
from __future__ import annotations

from pathlib import Path
import textwrap
import pytest

from pytead.testkit import setup as tk_setup, run_case as tk_run, param_ids as tk_ids


def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")


@pytest.fixture()
def mymod(tmp_path: Path):
    code = """
    class Box:
        __slots__ = ("x",)
        def __init__(self, x):
            self.x = x
        def inc(self, d):
            self.x += d
            return self.x

    def add(a, b):
        return a + b

    def transform(box, inc):
        # mutate input, then return a NEW Box based on the updated value
        box.x += inc
        return Box(box.x * 2)

    def make_box(x):
        return Box(x)
    """
    mod_path = tmp_path / "mymod.py"
    _write(mod_path, code)
    # Make tmp_path importable for the tests using the runtime setup hook
    tk_setup(__file__, [str(tmp_path)])
    return "mymod"

def test_inject_object_args_on_kw_and_pos(mymod):
    from pytead.rt import inject_object_args, rehydrate

    spec = {"type": f"{mymod}.Box", "state": {"x": 4}}

    # Cas kwargs : la clé existe déjà, elle doit être remplacée
    args, kwargs = (), {"box": None, "inc": 3}
    obj_args = {"pos": {}, "kw": {"box": spec}}
    a2, k2 = inject_object_args(args, kwargs, obj_args, None)
    assert a2 == ()
    assert "box" in k2 and getattr(k2["box"], "x", None) == 4

    # Cas positionnel : on fournit un slot à remplacer
    args, kwargs = (None,), {"inc": 3}
    obj_args = {"pos": {0: spec}, "kw": {}}
    a2, k2 = inject_object_args(args, kwargs, obj_args, None)
    assert len(a2) == 1 and getattr(a2[0], "x", None) == 4
    assert k2 == {"inc": 3}


def test_plain_function_add(mymod):
    # Case schema: (args, kwargs, expected, self_type, self_state, obj_args, result_spec)
    case = ((2, 3), {}, 5, None, None, None, None)
    tk_run(f"{mymod}.add", case)

    # Smoke check for id formatting
    ids = tk_ids([case])
    assert isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str)


def test_function_with_obj_args_and_result_obj(mymod):
    # transform(Box(x=4), inc=3) -> box.x devient 7, renvoie Box(x=14)
    obj_args = {"pos": {}, "kw": {"box": {"type": f"{mymod}.Box", "state": {"x": 4}}}}
    result_spec = {"type": f"{mymod}.Box", "state": {"x": 14}}
    # kwargs contient un placeholder (peu importe sa valeur), il sera remplacé
    case = ((), {"box": None, "inc": 3}, None, None, None, obj_args, result_spec)
    tk_run(f"{mymod}.transform", case)



def test_instance_method_with_self_placeholder(mymod):
    # Simulate a JSON/REPR trace where args[0] is a string placeholder for `self`;
    # testkit must drop it before calling the bound method.
    # Box(x=10).inc(5) -> 15
    placeholder = "<Box object at 0xDEADBEEF>"
    self_state = {"x": 10}
    case = ((placeholder, 5), {}, 15, f"{mymod}.Box", self_state, None, None)
    tk_run(f"{mymod}.Box.inc", case)

