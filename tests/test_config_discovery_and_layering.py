# tests/test_config_discovery_and_layering.py
from __future__ import annotations
from pathlib import Path
import argparse
import os
import runpy
import sys
import textwrap

import pytead.cli.config_cli as cfg


# -------- Helpers --------


class _DummyFiles:
    """Minimal shim to monkey-patch importlib.resources.files('pytead')."""

    def __init__(self, text: str):
        self._text = text

    def joinpath(self, name: str):
        return self

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text


def _write(p: Path, s: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")
    return p


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# dummy\n", encoding="utf-8")
    return p


# -------- User/local discovery (new CLI config module) --------


def test_find_user_config_precedence(tmp_path, monkeypatch):
    """
    _find_user_config must prefer:
      PYTEAD_CONFIG > XDG_CONFIG_HOME > ~/.config > ~/.pytead
    """
    # Reset HOME/XDG, and ensure no leakage
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

    # Lowest precedence
    home_cfg = _touch(home / ".pytead" / "config.toml")
    # Higher precedence
    home_cfg2 = _touch(home / ".config" / "pytead" / "config.toml")
    # XDG
    xdg_cfg = _touch(xdg / "pytead" / "config.toml")
    # Highest: explicit env
    explicit = _touch(tmp_path / "elsewhere" / "mycfg.toml")

    # 1) Without PYTEAD_CONFIG, XDG wins
    assert cfg._find_user_config() == xdg_cfg
    # 2) With PYTEAD_CONFIG set, it wins
    monkeypatch.setenv("PYTEAD_CONFIG", str(explicit))
    assert cfg._find_user_config() == explicit


def test_find_project_config_walks_up(tmp_path):
    """
    _find_project_config must return the nearest '.pytead/config.*' walking upward.
    """
    parent = tmp_path / "parent"
    proj = parent / "proj"
    deep = proj / "a" / "b"
    nearest = _touch(proj / ".pytead" / "config.toml")
    _touch(parent / ".pytead" / "config.toml")
    deep.mkdir(parents=True, exist_ok=True)

    found = cfg._find_project_config(deep)
    assert found == nearest


# -------- Layering tests: packaged < user < local --------


def test_layering_embedded_user_local_precedence(tmp_path, monkeypatch):
    """
    Check precedence:
      packaged defaults < user-level < project-local
    and that ctx.source_path anchors only to the local project config.
    """
    # Packaged defaults (via monkeypatch of cfg.ir.files)
    packaged = """
[defaults]
limit = 10
storage_dir = "call_logs"
format = "pickle"
[run]
limit = 7
"""
    monkeypatch.setattr(cfg.ir, "files", lambda pkg: _DummyFiles(packaged))

    # User-level (XDG) override
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    user_cfg = _write(
        xdg / "pytead" / "config.toml",
        """
        [defaults]
        format = "json"      # override packaged
        [run]
        limit = 8            # override packaged [run].limit
        """,
    )

    # Project-local override (wins over user)
    proj = tmp_path / "proj"
    proj.mkdir()
    local_cfg = _write(
        proj / ".pytead" / "config.toml",
        """
        [run]
        limit = 5
        """,
    )

    ctx = cfg.load_layered_config(start=proj)
    eff = cfg.effective_section(ctx, "run")

    assert eff["limit"] == 5  # local wins
    assert eff["format"] == "json"  # user wins over packaged
    assert str(eff["storage_dir"]).endswith("call_logs")  # inherited from packaged
    assert ctx.source_path == local_cfg  # anchor on local only
    assert ctx.project_root == proj.resolve()


def test_layering_user_only_when_no_local(tmp_path, monkeypatch):
    """
    Without local config, inherit packaged defaults overridden by user config.
    ctx.source_path remains None.
    """
    packaged = """
[defaults]
limit = 10
storage_dir = "call_logs"
format = "pickle"
[run]
limit = 7
"""
    monkeypatch.setattr(cfg.ir, "files", lambda pkg: _DummyFiles(packaged))

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

    # YAML user config (HOME/.config)
    _write(
        home / ".config" / "pytead" / "config.yaml",
        """
        defaults:
          format: repr
        run:
          limit: 9
        """,
    )

    ctx = cfg.load_layered_config(start=tmp_path / "work")
    eff = cfg.effective_section(ctx, "run")

    assert eff["limit"] == 9
    assert eff["format"] == "repr"
    assert str(eff["storage_dir"]).endswith("call_logs")
    assert ctx.source_path is None


# -------- apply_effective_to_args + discovery from script dir --------


def test_apply_config_fills_namespace(tmp_path, monkeypatch):
    """
    apply_effective_to_args must fill an empty Namespace with layered values.
    """
    packaged = """
[defaults]
storage_dir = "call_logs"
format = "pickle"
limit = 10
[run]
limit = 7
"""
    monkeypatch.setattr(cfg.ir, "files", lambda pkg: _DummyFiles(packaged))

    proj = tmp_path / "repo"
    proj.mkdir()
    _write(
        proj / ".pytead" / "config.toml",
        """
        [run]
        limit = 5
        targets = ["mymodule.multiply"]
        """,
    )

    ns = argparse.Namespace()  # simulate no CLI flags
    ctx = cfg.load_layered_config(start=proj)
    cfg.apply_effective_to_args("run", ctx, ns)

    assert ns.limit == 5
    assert ns.format == "pickle"
    assert str(ns.storage_dir).endswith("call_logs")
    assert ns.targets == ["mymodule.multiply"]


def test_cmd_run_discovers_config_from_script_dir(tmp_path, monkeypatch, caplog):
    """
    `pytead run` must look for config starting from the script's directory (start_hint),
    even if CWD is elsewhere. Avoid real execution via monkeypatch.
    """
    # Import the updated command module
    from pytead.cli import cmd_run

    # (optional but stabilizes the assertion surface)
    import logging

    caplog.set_level(logging.INFO, logger="pytead")

    # Packaged defaults (minimal)
    monkeypatch.setattr(
        cfg.ir,
        "files",
        lambda pkg: _DummyFiles(
            """
            [defaults]
            storage_dir = "call_logs"
            format = "pickle"
            limit = 10
            [run]
            limit = 7
            """
        ),
    )

    # Small repo
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write(
        repo / ".pytead" / "config.toml",
        """
        [run]
        limit = 6
        targets = ["mymodule.multiply"]
        """,
    )
    _write(
        repo / "mymodule.py",
        """
        def multiply(a, b): return a * b
        """,
    )
    script_path = _write(
        repo / "main.py",
        """
        from mymodule import multiply
        if __name__ == "__main__":
            multiply(2, 3)
        """,
    )

    # Ensure repo is NOT already on sys.path (we want to observe insertion)
    while str(repo) in sys.path:
        sys.path.remove(str(repo))

    # Neutralize actual script execution
    monkeypatch.setattr(runpy, "run_path", lambda *a, **k: None, raising=True)

    # Neutralize real instrumentation (return the targets as "seen")
    import pytead.targets as tg

    monkeypatch.setattr(
        tg, "instrument_targets", lambda targets, **kw: set(targets), raising=True
    )

    class NS:
        pass

    args = NS()
    args.targets = []  # empty -> fallback to [run].targets in project config
    args.cmd = [str(script_path)]  # like argparse.REMAINDER

    outside = tmp_path / "outside"
    outside.mkdir()
    old_cwd = Path.cwd()
    try:
        os.chdir(outside)
        cmd_run._handle(args)

        # We should have instrumented the config-provided target
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "Instrumentation applied" in log_text
        assert "mymodule.multiply" in log_text

        # And repo should have been added to sys.path by import env setup
        assert str(repo) in sys.path
    finally:
        os.chdir(old_cwd)
        # Clean sys.path pollution if any
        while str(repo) in sys.path:
            sys.path.remove(str(repo))


# -------- PYTEAD_CONFIG (YAML) --------


def test_env_yaml_via_pytead_config(tmp_path, monkeypatch):
    """
    PYTEAD_CONFIG may point to a YAML and must be honored.
    """
    # Packaged (to inherit missing fields)
    monkeypatch.setattr(
        cfg.ir,
        "files",
        lambda pkg: _DummyFiles(
            """
            [defaults]
            storage_dir = "call_logs"
            format = "pickle"
            limit = 10
            """
        ),
    )

    user_cfg = _write(
        tmp_path / "user_config.yaml",
        """
        defaults:
          format: repr
        run:
          limit: 9
          targets: ["pkg.mod.fn"]
        """,
    )
    monkeypatch.setenv("PYTEAD_CONFIG", str(user_cfg))

    ns = argparse.Namespace()
    ctx = cfg.load_layered_config(start=None)
    cfg.apply_effective_to_args("run", ctx, ns)

    assert ns.limit == 9
    assert ns.format == "repr"
    assert str(ns.storage_dir).endswith("call_logs")
    assert ns.targets == ["pkg.mod.fn"]
