# tests/test_sys_path_and_config.py
from __future__ import annotations
import os
import sys
import runpy
import logging
import importlib
from pathlib import Path
import textwrap
import pytest


# --------------------------- Helpers ---------------------------


def write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).lstrip() + "\n", encoding="utf-8")


def purge_modules(*names: str) -> None:
    for n in names:
        sys.modules.pop(n, None)


def make_repo_with_toml(tmp_path: Path):
    """
    repo/
      .pytead/config.toml         # targets + additional_sys_path
      ioutils.py                  # import via BASE PATH (racine repo)
      logical_entities/world.py   # import via additional_sys_path
      app/
        main.py                   # script
        adjacent_mod.py           # import via SCRIPT DIR
    """
    repo = tmp_path / "repo"
    app = repo / "app"
    le = repo / "logical_entities"
    (repo / ".pytead").mkdir(parents=True)
    app.mkdir(parents=True)
    le.mkdir(parents=True)

    # Config TOML : cibles + chemins additionnels
    write(
        repo / ".pytead" / "config.toml",
        """
        [defaults]
        limit = 7
        storage_dir = "call_logs"
        format = "pickle"

        [run]
        targets = [
          "ioutils.render_json",
          "world.BaseEntity.get_coordinates",
          "adjacent_mod.echo"
        ]
        additional_sys_path = ["./logical_entities"]
        """,
    )

    write(
        repo / "ioutils.py",
        """
        def render_json(x):
            return {"x": x}
        """,
    )

    write(
        le / "world.py",
        """
        class BaseEntity:
            def get_coordinates(self):
                return (0, 0)
        """,
    )

    write(
        app / "adjacent_mod.py",
        """
        def echo(x):
            return x
        """,
    )

    write(
        app / "main.py",
        """
        if __name__ == "__main__":
            pass
        """,
    )

    return repo, app / "main.py"


def make_repo_with_yaml(tmp_path: Path):
    """
    Même structure mais config YAML et targets pour TEAD.
    """
    repo = tmp_path / "repo_yaml"
    app = repo / "app"
    le = repo / "logical_entities"
    (repo / ".pytead").mkdir(parents=True)
    app.mkdir(parents=True)
    le.mkdir(parents=True)

    write(
        repo / ".pytead" / "config.yaml",
        """
        defaults:
          limit: 9
          storage_dir: "ylogs"
          format: "json"
        run:
          targets:
            - ioutils.render_json
            - adjacent_mod.echo
          additional_sys_path:
            - ./logical_entities
        """,
    )

    write(
        repo / "ioutils.py",
        """
        def render_json(x):
            return {"x": x}
        """,
    )

    write(
        le / "world.py",
        """
        class BaseEntity:
            def get_coordinates(self):
                return (1, 2)
        """,
    )

    write(
        app / "adjacent_mod.py",
        """
        def echo(x):
            return x
        """,
    )

    write(
        app / "main.py",
        """
        if __name__ == "__main__":
            pass
        """,
    )

    return repo, app / "main.py"


@pytest.fixture(autouse=True)
def clean_sys_path_and_modules():
    """Nettoyage entre tests."""
    before = list(sys.path)
    purge_modules("ioutils", "world", "adjacent_mod", "exmod")
    yield
    sys.path[:] = before
    purge_modules("ioutils", "world", "adjacent_mod", "exmod")


def no_op_run_path(*a, **k):
    return None


# --------------------------- Tests ---------------------------


def test_cmd_run_injects_paths_and_imports_targets(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pytead")
    repo, script = make_repo_with_toml(tmp_path)

    # Simule un lancement hors repo
    outside = tmp_path / "outside"
    outside.mkdir()

    # Évite l’exécution réelle du script
    monkeypatch.setattr(runpy, "run_path", no_op_run_path, raising=True)

    # No-op instrumentation : patcher l'endroit correct
    import pytead.targets as tg
    monkeypatch.setattr(
        tg, "instrument_targets", lambda targets, **kw: set(targets), raising=True
    )

    import pytead.cli.cmd_run as cmd_run

    # Prépare args type argparse
    class NS:
        pass

    args = NS()
    args.targets = []  # seront lues depuis la config TOML
    args.cmd = [str(script)]

    # Exécute depuis un dossier extérieur
    old_cwd = Path.cwd()
    try:
        os.chdir(outside)
        cmd_run._handle(args)

        # 1) Vérifications d’import réelles
        mod_iou = importlib.import_module("ioutils")
        assert callable(getattr(mod_iou, "render_json", None))
        assert mod_iou.render_json(3) == {"x": 3}

        mod_world = importlib.import_module("world")
        assert hasattr(mod_world, "BaseEntity")
        assert mod_world.BaseEntity().get_coordinates() == (0, 0)

        mod_adj = importlib.import_module("adjacent_mod")
        assert mod_adj.echo("ok") == "ok"

        # 2) Chemins attendus
        assert str((repo / "app").resolve()) in sys.path  # script dir
        assert str(repo.resolve()) in sys.path            # base path
        assert str((repo / "logical_entities").resolve()) in sys.path  # additional_sys_path

        # 3) Logs d’instrumentation
        log_text = "\n".join(r.message for r in caplog.records)
        assert "Instrumentation applied to 3 target(s)" in log_text
        assert "ioutils.render_json" in log_text
        assert "world.BaseEntity.get_coordinates" in log_text
        assert "adjacent_mod.echo" in log_text
    finally:
        os.chdir(old_cwd)


def test_cmd_run_resolves_relative_additional_sys_path(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pytead")
    repo = tmp_path / "repo_rel"
    app = repo / "app"
    extras = repo / "extras"
    (repo / ".pytead").mkdir(parents=True)
    app.mkdir(parents=True)
    extras.mkdir(parents=True)

    write(
        repo / ".pytead" / "config.toml",
        """
        [defaults]
        limit = 5
        storage_dir = "call_logs"
        format = "pickle"

        [run]
        targets = ["exmod.ping"]
        additional_sys_path = ["./extras"]
        """,
    )

    write(
        extras / "exmod.py",
        """
        def ping():
            return "pong"
        """,
    )

    write(app / "main.py", "pass")

    # No-op exécution & instrumentation
    monkeypatch.setattr(runpy, "run_path", no_op_run_path, raising=True)
    import pytead.targets as tg
    monkeypatch.setattr(
        tg, "instrument_targets", lambda targets, **kw: set(targets), raising=True
    )

    import pytead.cli.cmd_run as cmd_run

    class NS:
        pass

    args = NS()
    args.targets = []
    args.cmd = [str(app / "main.py")]

    old_cwd = Path.cwd()
    outside = tmp_path / "outside2"
    outside.mkdir()
    try:
        os.chdir(outside)
        cmd_run._handle(args)

        # Import réel
        mod_ex = importlib.import_module("exmod")
        assert mod_ex.ping() == "pong"

        # chemin additional résolu par rapport à la racine du projet
        assert str((repo / "extras").resolve()) in sys.path

        # log instrumentation
        log_text = "\n".join(r.message for r in caplog.records)
        assert "Instrumentation applied to 1 target(s)" in log_text
        assert "exmod.ping" in log_text
    finally:
        os.chdir(old_cwd)


def test_tead_injects_paths_and_imports_from_yaml(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pytead")
    repo, script = make_repo_with_yaml(tmp_path)

    # No-op exécution & instrumentation
    monkeypatch.setattr(runpy, "run_path", no_op_run_path, raising=True)
    import pytead.targets as tg
    monkeypatch.setattr(
        tg, "instrument_targets", lambda targets, **kw: set(targets), raising=True
    )

    import pytead.cli.cmd_tead as tead

    class NS:
        pass

    args = NS()
    args.targets = []  # pris depuis la config 'run'
    args.cmd = [str(script)]

    old_cwd = Path.cwd()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    try:
        os.chdir(outside)
        tead.run(args)

        # Imports réels
        mod_iou = importlib.import_module("ioutils")
        assert mod_iou.render_json(1) == {"x": 1}
        mod_adj = importlib.import_module("adjacent_mod")
        assert mod_adj.echo("z") == "z"

        # Chemins présents
        assert str((repo / "app").resolve()) in sys.path  # script dir
        assert str(repo.resolve()) in sys.path            # base path
        assert str((repo / "logical_entities").resolve()) in sys.path  # additional_sys_path

        # Logs (au moins une des cibles doit apparaître)
        log_text = "\n".join(r.message for r in caplog.records)
        assert ("ioutils.render_json" in log_text) or ("adjacent_mod.echo" in log_text)
    finally:
        os.chdir(old_cwd)

