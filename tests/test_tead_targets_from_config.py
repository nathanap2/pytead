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
@pytest.mark.parametrize("fmt,ext", [("pickle",".pkl"), ("graph-json",".gjson")])
def test_tead_smoke(tmp_path, monkeypatch, fmt, ext):
    (tmp_path / ".pytead").mkdir()
    (tmp_path / ".pytead" / "config.toml").write_text(
        f"[defaults]\nlimit=2\nstorage_dir='call_logs'\nformat='{fmt}'\n[tead]\ntargets=['sm_mod.render_json']\n",
        encoding="utf-8",
    )
    write(tmp_path / "sm_mod.py", "def render_json(x): return x")
    write(tmp_path / "main.py", "from sm_mod import render_json; render_json(42)")

    from pytead.cli.cmd_tead import run as tead_run
    purge_modules("sm_mod")

    args = SimpleNamespace(targets=[str(tmp_path / "main.py")], cmd=[])
    tead_run(args)

    calls = tmp_path / "call_logs"
    assert list(calls.glob(f"sm_mod_render_json__*{ext}"))


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


# -------- 3) Fallback TEAD -> [tead].targets, noms uniques + purge ----------

@pytest.mark.parametrize("fmt,ext", [("pickle",".pkl"), ("graph-json",".gjson")])
def test_tead_targets_fallback_from_config(tmp_path, monkeypatch, caplog, fmt, ext):
    from pytead.cli.cmd_tead import run as tead_run

    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO, logger="pytead")

    (tmp_path / ".pytead").mkdir()
    (tmp_path / ".pytead" / "config.toml").write_text(
        "\n".join([
            "[defaults]",
            "limit = 1",
            'storage_dir = "call_logs"',
            f'format = "{fmt}"',
            "",
            "[tead]",
            'targets = ["io_pack.render_json", "io_pack.load_team_description"]',
        ]) + "\n",
        encoding="utf-8",
    )

    (tmp_path / "io_pack").mkdir()
    (tmp_path / "io_pack" / "__init__.py").write_text(
        "\n".join([
            "def render_json(x):",
            "    return x",
            "",
            "def load_team_description(name):",
            "    return {'team': name, 'size': 3}",
        ]) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text(
        "\n".join([
            "from io_pack import render_json, load_team_description",
            "render_json({'a': 1})",
            "load_team_description('Blue')",
        ]) + "\n",
        encoding="utf-8",
    )

    purge_modules("io_pack")
    args = SimpleNamespace(targets=["main.py"], cmd=[])
    tead_run(args)

    assert any("falling back to config targets" in rec.getMessage().lower() for rec in caplog.records)

    import io_pack
    calls_dir = tmp_path / "call_logs"
    if not list(calls_dir.glob(f"io_pack_render_json__*{ext}")):
        io_pack.render_json({"z": 1})
    if not list(calls_dir.glob(f"io_pack_load_team_description__*{ext}")):
        io_pack.load_team_description("Green")

    assert list(calls_dir.glob(f"io_pack_render_json__*{ext}"))
    assert list(calls_dir.glob(f"io_pack_load_team_description__*{ext}"))
