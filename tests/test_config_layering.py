# tests/test_config_layering.py
from pathlib import Path
import argparse
import os
import pytead.config as cfg


class _DummyFiles:
    def __init__(self, text: str):
        self._text = text

    def joinpath(self, name: str):
        return self

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_layering_embedded_user_local(tmp_path, monkeypatch):
    # 0) Packaged defaults
    packaged = """
[defaults]
limit = 10
storage_dir = "call_logs"
format = "pickle"
[run]
limit = 7
"""
    monkeypatch.setattr(cfg.ir, "files", lambda pkg: _DummyFiles(packaged))

    # 1) User-level (global) config under XDG_CONFIG_HOME
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    user_cfg = xdg_home / "pytead" / "config.toml"
    _write(
        user_cfg,
        """
[defaults]
format = "json"      # overrides packaged default
[run]
limit = 8            # overrides packaged [run].limit
""",
    )

    # 2) Local (project) config
    proj = tmp_path / "proj"
    local_cfg = proj / ".pytead" / "config.toml"
    _write(
        local_cfg,
        """
[run]
limit = 5            # overrides user-level [run].limit
""",
    )

    eff = cfg.get_effective_config("run", start=proj)

    # Precedence: packaged < user < local
    assert eff["limit"] == 5  # local wins
    assert eff["format"] == "json"  # user-level wins over packaged
    assert str(eff["storage_dir"]).endswith("call_logs")  # inherited from packaged
    # LAST_CONFIG_PATH anchors to local only
    assert cfg.LAST_CONFIG_PATH == local_cfg


def test_apply_config_fills_namespace_with_layered_values(tmp_path, monkeypatch):
    packaged = """
[defaults]
storage_dir = "call_logs"
format = "pickle"
limit = 10
[run]
limit = 7
"""
    # Fausse les defaults packagées
    monkeypatch.setattr(cfg.ir, "files", lambda pkg: _DummyFiles(packaged))

    # Contexte user-only (pas de locale) : HOME doit pointer vers le répertoire où l'on écrit
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

    # ~/.config/pytead/config.yaml (user-level)
    home_cfg = home / ".config" / "pytead" / "config.yaml"
    _write(
        home_cfg,
        """
defaults:
  format: repr
run:
  limit: 9
""",
    )

    # Pas de config locale
    proj = tmp_path / "proj2"
    proj.mkdir()

    # Namespace vide → rempli depuis le layering (packaged < user)
    ns = argparse.Namespace()
    cfg.apply_config_from_default_file("run", ns, start=proj)

    # Vérifications
    assert ns.limit == 9
    assert ns.format == "repr"
    assert str(ns.storage_dir).endswith("call_logs")
    # Pas de config locale détectée → pas d'ancrage projet
    assert cfg.LAST_CONFIG_PATH is None
