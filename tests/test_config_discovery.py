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
    
    


import os
import runpy
import sys
import textwrap
from pathlib import Path

import pytest

# --- Helpers -----------------------------------------------------------------

def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")


def _make_min_repo_with_toml(repo: Path) -> tuple[Path, Path, Path]:
    """
    Create a minimal repo layout with:
      - .pytead/config.toml
      - mymodule.py (with multiply)
      - main.py (calls multiply)
    Returns (config_path, module_path, script_path).
    """
    cfg = repo / ".pytead" / "config.toml"
    _write(
        cfg,
        """
        [defaults]
        limit = 7
        storage_dir = "call_logs"
        format = "pickle"

        [run]
        targets = ["mymodule.multiply"]
        """,
    )

    _write(
        repo / "mymodule.py",
        """
        def multiply(a, b):
            return a * b
        """,
    )

    _write(
        repo / "main.py",
        """
        from mymodule import multiply

        if __name__ == "__main__":
            for (x, y) in [(2, 3), (10, 0)]:
                multiply(x, y)
        """,
    )
    return cfg, repo / "mymodule.py", repo / "main.py"


def _make_min_repo_with_yaml(repo: Path) -> Path:
    """
    Create a minimal repo with .pytead/config.yaml
    """
    cfg = repo / ".pytead" / "config.yaml"
    _write(
        cfg,
        """
        defaults:
          limit: 11
          storage_dir: "ylogs"
          format: "json"
        run:
          targets:
            - "mymodule.multiply"
        """,
    )
    return cfg


# --- Tests -------------------------------------------------------------------

def test_run_uses_script_dir_for_config_toml(tmp_path, monkeypatch, caplog):
    """
    Ensure cmd_run._handle discovers .pytead/config.toml starting from the script directory,
    even when the current working directory is outside the repo.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path, module_path, script_path = _make_min_repo_with_toml(repo)

    # Create an "outside" working directory and chdir there to simulate running pytead from elsewhere
    outside = tmp_path / "outside"
    outside.mkdir()

    # Prepend the repo to sys.path so the target module can be imported even though CWD is outside.
    sys.path.insert(0, str(repo))

    # Patch run_path so we don't actually execute the script logic during the test
    monkeypatch.setattr(runpy, "run_path", lambda *a, **k: None, raising=True)

    # Patch tracing.trace to a no-op decorator to avoid file I/O during instrumentation
    import pytead.cmd_run as cmd_run
    def _noop_trace(**_kwargs):
        def _dec(fn):
            return fn
        return _dec
    monkeypatch.setattr(cmd_run, "trace", _noop_trace, raising=True)

    # Build a Namespace mimicking argparse output: no CLI-provided config; only the script path in cmd.
    class NS:
        pass
    args = NS()
    args.targets = []     # empty → should fallback to config [run].targets
    args.cmd = [str(script_path)]  # what argparse.REMAINDER would give (no leading '--')

    # Run from "outside"
    old_cwd = Path.cwd()
    try:
        os.chdir(outside)
        # Import here to get LAST_CONFIG_PATH
        from pytead.config import LAST_CONFIG_PATH
        # Execute handler; should load config from repo/.pytead/config.toml thanks to start_hint
        cmd_run._handle(args)

        # Assert that the config file used is the repo one
        from pytead import config as _cfgmod  # re-import to read the mutated global
        assert _cfgmod.LAST_CONFIG_PATH == config_path

        # Optional: check logs mention instrumentation and the target name
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "Instrumentation applied" in log_text
        assert "mymodule.multiply" in log_text
    finally:
        os.chdir(old_cwd)
        # Clean sys.path pollution
        if str(repo) in sys.path:
            sys.path.remove(str(repo))


def test_yaml_config_loading_with_apply(tmp_path):
    """
    Ensure YAML config (.pytead/config.yaml) is parsed and fills args fields via apply_config_from_default_file.
    """
    repo = tmp_path / "proj_yaml"
    repo.mkdir()
    cfg_yaml = _make_min_repo_with_yaml(repo)

    from argparse import Namespace
    from pytead.config import apply_config_from_default_file, get_effective_config

    args = Namespace()
    # No attributes set → they should be filled from YAML
    apply_config_from_default_file("run", args, start=repo)

    # Types are coerced: paths become Path objects, ints become int, lists normalized.
    assert getattr(args, "limit", None) == 11
    assert getattr(args, "storage_dir", None) == Path("ylogs")
    assert getattr(args, "format", None) == "json"
    assert getattr(args, "targets", None) == ["mymodule.multiply"]

    eff = get_effective_config("run", start=repo)
    assert eff["limit"] == 11
    assert eff["storage_dir"] == Path("ylogs")
    assert eff["format"] == "json"
    assert eff["targets"] == ["mymodule.multiply"]


def test_env_yaml_via_pytead_config(tmp_path, monkeypatch):
    """
    Ensure PYTEAD_CONFIG can point to a YAML file and is honored.
    """
    user_cfg = tmp_path / "user_config.yaml"
    _write(
        user_cfg,
        """
        defaults:
          limit: 5
          storage_dir: "ucalls"
          format: "repr"
        run:
          targets: ["pkg.mod.fn"]
        """,
    )

    monkeypatch.setenv("PYTEAD_CONFIG", str(user_cfg))

    from argparse import Namespace
    from pytead.config import apply_config_from_default_file

    args = Namespace()
    # start=None on purpose; discovery should use PYTEAD_CONFIG
    apply_config_from_default_file("run", args, start=None)

    assert getattr(args, "limit", None) == 5
    assert getattr(args, "storage_dir", None) == Path("ucalls")
    assert getattr(args, "format", None) == "repr"
    assert getattr(args, "targets", None) == ["pkg.mod.fn"]

