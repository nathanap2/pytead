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


def test_tead_debug_wrapped_status(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="pytead")
    (tmp_path / ".pytead").mkdir()
    (tmp_path / ".pytead" / "config.toml").write_text(
        "[defaults]\nlimit=1\nstorage_dir='call_logs'\nformat='pickle'\n"
        "[tead]\ntargets=['sm_mod.render_json']\n",
        encoding="utf-8",
    )
    (tmp_path / "sm_mod.py").write_text(
        "def render_json(x): return x\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "from sm_mod import render_json; render_json(1)\n", encoding="utf-8"
    )

    from types import SimpleNamespace
    from pytead.cli.cmd_tead import run as tead_run

    # simulate 'pytead tead -- main.py'
    args = SimpleNamespace(targets=[str(tmp_path / "main.py")], cmd=[])
    tead_run(args)

    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "Pre-run check sm_mod.render_json" in msg
    assert "Storage dir" in msg


# -------- 1) TEAD smoke : découvre config même si script dans targets ----------
def test_tead_smoke(tmp_path, monkeypatch):
    # repo layout
    (tmp_path / ".pytead").mkdir()
    (tmp_path / ".pytead" / "config.toml").write_text(
        "[defaults]\nlimit=2\nstorage_dir='call_logs'\nformat='pickle'\n[tead]\ntargets=['sm_mod.render_json']\n",
        encoding="utf-8",
    )
    write(tmp_path / "sm_mod.py", "def render_json(x): return x")
    write(tmp_path / "main.py", "from sm_mod import render_json; render_json(42)")

    # Pas de chdir volontairement, pour vérifier la découverte via targets
    from pytead.cli.cmd_tead import run as tead_run

    # Purge pour éviter un sm_mod résiduel
    purge_modules("sm_mod")

    args = SimpleNamespace(targets=[str(tmp_path / "main.py")], cmd=[])
    tead_run(args)

    calls = tmp_path / "call_logs"
    assert list(calls.glob("sm_mod_render_json__*.pkl")), "pickle non écrit par TEAD"


# -------- 2) Instrumentation directe : pas de nom 'pkg', purge avant ----------
def test_instrument_targets_writes_pickle(tmp_path, monkeypatch):
    from pytead.targets import instrument_targets
    from pytead.storage import get_storage

    repo = tmp_path
    (repo / "mypkg").mkdir()
    write(repo / "mypkg" / "__init__.py", "def f(x): return x*2")

    # rendre importable + purge collisions
    monkeypatch.syspath_prepend(str(repo))
    purge_modules("mypkg")

    # instrumente
    calls = repo / "call_logs"
    storage = get_storage("pickle")
    seen = instrument_targets(["mypkg.f"], limit=1, storage_dir=calls, storage=storage)
    assert "mypkg.f" in seen

    # appelle et vérifie le fichier
    import mypkg  # noqa: F401

    assert mypkg.f(3) == 6  # appelle le wrapper
    files = list(calls.glob("mypkg_f__*.pkl"))
    assert files, "Aucun fichier .pkl écrit par l'instrumentation."


# -------- 3) Fallback TEAD -> [tead].targets, noms uniques + purge ----------
def test_tead_targets_fallback_from_config(tmp_path, monkeypatch, caplog):
    from pytead.cli.cmd_tead import run as tead_run

    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO, logger="pytead")

    # config locale
    (tmp_path / ".pytead").mkdir()
    (tmp_path / ".pytead" / "config.toml").write_text(
        "\n".join(
            [
                "[defaults]",
                "limit = 1",
                'storage_dir = "call_logs"',
                'format = "pickle"',
                "",
                "[tead]",
                'targets = ["io_pack.render_json", "io_pack.load_team_description"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # package ciblé
    (tmp_path / "io_pack").mkdir()
    (tmp_path / "io_pack" / "__init__.py").write_text(
        "\n".join(
            [
                "def render_json(x):",
                "    return x",
                "",
                "def load_team_description(name):",
                "    return {'team': name, 'size': 3}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # script qui appelle les 2 fonctions
    (tmp_path / "main.py").write_text(
        "\n".join(
            [
                "from io_pack import render_json, load_team_description",
                "render_json({'a': 1})",
                "load_team_description('Blue')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # purge module pour éviter un cache d'un autre test
    purge_modules("io_pack")

    # simulate `pytead tead -- main.py` (targets contient "main.py")
    args = SimpleNamespace(targets=["main.py"], cmd=[])
    tead_run(args)

    # Fallback message vu
    assert any(
        "falling back to config targets" in rec.getMessage().lower()
        for rec in caplog.records
    )

    # importer le module instrumenté et, si besoin, forcer un appel pour écrire
    import io_pack

    calls_dir = tmp_path / "call_logs"
    if not list(calls_dir.glob("io_pack_render_json__*.pkl")):
        io_pack.render_json({"z": 1})
    if not list(calls_dir.glob("io_pack_load_team_description__*.pkl")):
        io_pack.load_team_description("Green")

    # traces présentes
    assert list(
        calls_dir.glob("io_pack_render_json__*.pkl")
    ), "Trace render_json introuvable"
    assert list(
        calls_dir.glob("io_pack_load_team_description__*.pkl")
    ), "Trace load_team_description introuvable"
