# tests/test_config_layering.py
from pathlib import Path
import argparse
import pytead.cli.config_cli as cfg


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

    # Load layered config starting from project dir
    ctx = cfg.load_layered_config(start=proj)
    eff = cfg.effective_section(ctx, "run")

    # Precedence: packaged < user < local
    assert eff["limit"] == 5  # local wins
    assert eff["format"] == "json"  # user-level wins over packaged
    assert str(eff["storage_dir"]).endswith("call_logs")  # inherited from packaged

    # Project anchoring information lives in the context now
    assert ctx.source_path == local_cfg
    assert ctx.project_root == proj.resolve()


def test_apply_config_fills_namespace_with_layered_values(tmp_path, monkeypatch):
    # Packaged defaults (read via importlib.resources.files)
    packaged = """
[defaults]
storage_dir = "call_logs"
format = "pickle"
limit = 10
[run]
limit = 7
"""
    monkeypatch.setattr(cfg.ir, "files", lambda pkg: _DummyFiles(packaged))

    # User-level: ~/.config/pytead/config.yaml
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PYTEAD_CONFIG", raising=False)

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

    # No local (project) config
    proj = tmp_path / "proj2"
    proj.mkdir()

    # Empty Namespace -> filled from layered config (packaged < user)
    ns = argparse.Namespace()
    ctx = cfg.load_layered_config(start=proj)
    cfg.apply_effective_to_args("run", ctx, ns)

    # Verifications
    assert ns.limit == 9
    assert ns.format == "repr"
    assert str(ns.storage_dir).endswith("call_logs")

    # No local config detected -> no project anchoring file
    assert ctx.source_path is None

