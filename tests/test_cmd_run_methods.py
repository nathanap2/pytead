from types import SimpleNamespace
from pathlib import Path
import textwrap

from pytead.cmd_run import _handle as run_handle
from pytead.storage import iter_entries

def test_cmd_run_resolves_and_wraps_methods(tmp_path, monkeypatch, caplog):
    """
    Phase 2: the 'run' command must accept module.Class.method targets and
    instrument them descriptor-aware (instance/static/class).
    We run a tiny script that calls each, and then assert traces exist.
    """
    monkeypatch.chdir(tmp_path)

    # --- Create a tiny package with a class and methods ---
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "models.py").write_text(
        textwrap.dedent(
            """
            class Calc:
                @staticmethod
                def smul(a, b):
                    return a * b

                @classmethod
                def tag(cls, s):
                    return f"{cls.__name__}:{s}"

                def add(self, a, b):
                    return a + b
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # --- Script that exercises the three variants ---
    (tmp_path / "main.py").write_text(
        textwrap.dedent(
            """
            from pkg.models import Calc
            Calc.smul(2, 5)
            Calc.tag("ok")
            Calc().add(3, 4)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    calls_dir = tmp_path / "logs"

    # Simulate argparse-parsed args for `pytead run`
    args = SimpleNamespace(
        limit=10,
        storage_dir=calls_dir,
        format="pickle",
        targets=["pkg.models.Calc.smul", "pkg.models.Calc.tag", "pkg.models.Calc.add"],
        cmd=["main.py"],
    )

    caplog.set_level("INFO")
    run_handle(args)

    # --- Files were written for each method (prefix uses module.Class.method) ---
    assert list(calls_dir.glob("pkg_models_Calc_smul__*.pkl")), "missing smul trace"
    assert list(calls_dir.glob("pkg_models_Calc_tag__*.pkl")), "missing tag trace"
    assert list(calls_dir.glob("pkg_models_Calc_add__*.pkl")), "missing add trace"

    # --- Entries decode properly and carry the fully-qualified func name ---
    entries = list(iter_entries(calls_dir, formats=["pickle"]))
    funcs = sorted(e["func"] for e in entries)
    assert any(f.endswith(".Calc.smul") for f in funcs)
    assert any(f.endswith(".Calc.tag") for f in funcs)
    assert any(f.endswith(".Calc.add") for f in funcs)

    # Quick sanity on recorded results
    byf = {e["func"]: e for e in entries}
    k_smul = next(k for k in byf if k.endswith(".Calc.smul"))
    k_tag = next(k for k in byf if k.endswith(".Calc.tag"))
    k_add = next(k for k in byf if k.endswith(".Calc.add"))
    assert byf[k_smul]["result"] == 10
    assert byf[k_tag]["result"] == "Calc:ok"
    assert byf[k_add]["result"] == 7

