# tests/test_config_discovery_and_layering.py
from __future__ import annotations
from pathlib import Path
import argparse
import os
import runpy
import sys
import textwrap

import pytead.config as cfg


# -------- Helpers --------


class _DummyFiles:
    """Shim minimal pour monkey-patcher importlib.resources.files('pytead')."""

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


# -------- Tests de découverte user/local (nouvelles fonctions) --------


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


# -------- Tests de layering embarqué < user < local --------


def test_layering_embedded_user_local_precedence(tmp_path, monkeypatch):
    """
    Vérifie l'empilement et la priorité:
      packaged defaults < user-level < project-local
    et LAST_CONFIG_PATH qui n'ancre que la locale.
    """
    # Packaged defaults (via monkeypatch de cfg.ir.files)
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

    eff = cfg.get_effective_config("run", start=proj)

    assert eff["limit"] == 5  # local wins
    assert eff["format"] == "json"  # user wins over packaged
    assert str(eff["storage_dir"]).endswith("call_logs")  # inherited from packaged
    assert cfg.LAST_CONFIG_PATH == local_cfg  # anchor on local only


def test_layering_user_only_when_no_local(tmp_path, monkeypatch):
    """
    Sans config locale, on doit hériter des defaults packagés,
    surchargés par la config user, et LAST_CONFIG_PATH reste None.
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
    # YAML user config (HOME/.config)
    user_yaml = _write(
        home / ".config" / "pytead" / "config.yaml",
        """
        defaults:
          format: repr
        run:
          limit: 9
        """,
    )

    eff = cfg.get_effective_config("run", start=tmp_path / "work")

    assert eff["limit"] == 9
    assert eff["format"] == "repr"
    assert str(eff["storage_dir"]).endswith("call_logs")
    assert cfg.LAST_CONFIG_PATH is None


# -------- Tests apply_config + découverte depuis le dossier du script --------


def test_apply_config_fills_namespace(tmp_path, monkeypatch):
    """
    apply_config_from_default_file doit remplir un Namespace vide
    avec les valeurs issues du layering.
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
    cfg.apply_config_from_default_file("run", ns, start=proj)

    assert ns.limit == 5
    assert ns.format == "pickle"
    assert str(ns.storage_dir).endswith("call_logs")
    assert ns.targets == ["mymodule.multiply"]


def test_cmd_run_discovers_config_from_script_dir(tmp_path, monkeypatch, caplog):
    """
    `pytead run` doit chercher la config à partir du dossier du script (start_hint),
    même si le CWD est ailleurs. On évite l’exécution réelle avec des monkeypatch.
    """
    from pytead import cmd_run

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

    # Petit repo
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

    # CWD à l’extérieur
    outside = tmp_path / "outside"
    outside.mkdir()
    # Import du module du repo
    sys.path.insert(0, str(repo))

    # Neutraliser l’exécution réelle du script et l’écriture de traces
    monkeypatch.setattr(runpy, "run_path", lambda *a, **k: None, raising=True)

    def _noop_trace(**_kwargs):
        def _dec(fn):
            return fn

        return _dec

    monkeypatch.setattr(cmd_run, "trace", _noop_trace, raising=True)

    class NS:  # Namespace simulé par argparse
        pass

    args = NS()
    args.targets = []  # vide → fallback config [run].targets
    args.cmd = [str(script_path)]  # comme argparse.REMAINDER

    old_cwd = Path.cwd()
    try:
        os.chdir(outside)
        cmd_run._handle(args)

        # Vérifie qu’on a bien pris la config du repo
        from pytead import config as _cfgmod

        assert _cfgmod.LAST_CONFIG_PATH == config_path

        # Vérifie qu’on a instrumenté la cible
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "Instrumentation applied" in log_text
        assert "mymodule.multiply" in log_text
    finally:
        os.chdir(old_cwd)
        if str(repo) in sys.path:
            sys.path.remove(str(repo))


# -------- Tests PYTEAD_CONFIG (YAML) --------


def test_env_yaml_via_pytead_config(tmp_path, monkeypatch):
    """
    PYTEAD_CONFIG peut pointer vers un YAML et doit être honoré.
    """
    # Packaged pour hériter des champs non définis
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

    from pytead.config import apply_config_from_default_file

    ns = argparse.Namespace()
    apply_config_from_default_file("run", ns, start=None)

    assert ns.limit == 9
    assert ns.format == "repr"
    assert str(ns.storage_dir).endswith("call_logs")
    assert ns.targets == ["pkg.mod.fn"]
