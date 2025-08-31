from types import SimpleNamespace
from pathlib import Path
import importlib
import logging
import sys
import textwrap
import inspect
import pytest

# ------- helpers -------
def write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).lstrip() + "\n", encoding="utf-8")


def purge_modules(*names: str) -> None:
    for n in names:
        sys.modules.pop(n, None)


def is_wrapped(obj):
    if hasattr(obj, "__wrapped__"):
        return True
    if hasattr(obj, "__func__") and hasattr(obj.__func__, "__wrapped__"):
        return True
    return inspect.unwrap(obj) is not obj




@pytest.mark.parametrize("storage_name,ext", [("pickle", ".pkl"), ("graph-json", ".gjson")])
def test_instrument_targets_writes(tmp_path, monkeypatch, storage_name, ext):
    from pytead.targets import instrument_targets
    from pytead.storage import get_storage

    repo = tmp_path
    (repo / "mypkg").mkdir()
    write(repo / "mypkg" / "__init__.py", "def f(x): return x*2")

    monkeypatch.syspath_prepend(str(repo))
    purge_modules("mypkg")

    calls = repo / "call_logs"
    storage = get_storage(storage_name)
    seen = instrument_targets(["mypkg.f"], limit=1, storage_dir=calls, storage=storage)
    assert "mypkg.f" in seen

    import mypkg
    assert mypkg.f(3) == 6
    assert list(calls.glob(f"mypkg_f__*{ext}"))



