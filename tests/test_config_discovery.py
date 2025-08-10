from pathlib import Path
import os

import pytest

from pytead.config import _find_default_config


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# dummy\n", encoding="utf-8")
    return p


def test_project_dotpytead_default_config_wins_over_parent_and_user(
    tmp_path: Path, monkeypatch
):
    """
    When both a project-local .pytead/config.toml and a parent-level file exist,
    the nearest project-local file must be selected.
    """
    # Fake HOME and XDG to avoid leaking host config
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

    # Parent-level project (simulate a monorepo parent)
    parent_proj = tmp_path / "parent_proj"
    proj = parent_proj / "myproj"
    deep = proj / "src" / "pkg"

    # Parent-level config (should be ignored in favor of project-local)
    _touch(parent_proj / ".pytead" / "config.toml")

    # Project-level config (nearest wins)
    chosen = _touch(proj / ".pytead" / "config.toml")

    # Also create user-level configs (should be ignored)
    _touch(xdg / "pytead" / "config.toml")
    _touch(home / ".config" / "pytead" / "config.toml")
    _touch(home / ".pytead" / "config.toml")

    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    found = _find_default_config(Path.cwd())
    assert found == chosen


def test_project_dotpytead_config_toml_used_if_default_missing(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

    proj = tmp_path / "p"
    deep = proj / "app"
    chosen = _touch(proj / ".pytead" / "config.toml")

    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    found = _find_default_config(Path.cwd())
    assert found == chosen


def test_env_var_used_when_no_project_config(tmp_path: Path, monkeypatch):
    """
    If no project-local config is present, PYTEAD_CONFIG should be used if it points to a file.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    proj = tmp_path / "proj"
    work = proj / "w"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)

    explicit = _touch(tmp_path / "elsewhere" / "mycfg.toml")
    monkeypatch.setenv("PYTEAD_CONFIG", str(explicit))

    found = _find_default_config(Path.cwd())
    assert found == explicit


def test_xdg_beats_home_configs_when_no_project_or_env(tmp_path: Path, monkeypatch):
    """
    If no project-local config and no PYTEAD_CONFIG, prefer $XDG_CONFIG_HOME/pytead/config.toml
    over ~/.config/pytead/config.toml and ~/.pytead/config.toml.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

    # Create all user-level candidates
    xdg_cfg = _touch(xdg / "pytead" / "config.toml")
    _touch(home / ".config" / "pytead" / "config.toml")
    _touch(home / ".pytead" / "config.toml")

    # Project without .pytead
    proj = tmp_path / "proj"
    deep = proj / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    found = _find_default_config(Path.cwd())
    assert found == xdg_cfg


def test_project_config_wins_even_if_env_is_set(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    proj = tmp_path / "proj"
    deep = proj / "d1" / "d2"
    chosen = _touch(proj / ".pytead" / "config.toml")
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    explicit = _touch(tmp_path / "else" / "cfg.toml")
    monkeypatch.setenv("PYTEAD_CONFIG", str(explicit))

    found = _find_default_config(Path.cwd())
    assert found == chosen
