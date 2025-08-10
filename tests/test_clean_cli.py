import sys
from pathlib import Path
from typing import Iterable, Tuple

import pytest

from pytead import cli
from pytead.storage import PickleStorage, JsonStorage, ReprStorage
from pytead.clean import run as clean_run

import json
import logging
from argparse import Namespace


def _mk_entry(func: str, ts: str = "2025-08-08T12:00:00Z"):
    return {
        "trace_schema": "pytead/v1",
        "func": func,
        "args": (1, 2),
        "kwargs": {},
        "result": 3,
        "timestamp": ts,
    }


def _write_traces(
    calls_dir: Path,
    specs: Iterable[Tuple[str, str, str]],
):
    """
    specs: iterable de tuples (func_fullname, storage_name, iso_timestamp)
           storage_name ∈ {"pickle","json","repr"}
    """
    name2storage = {
        "pickle": PickleStorage(),
        "json": JsonStorage(),
        "repr": ReprStorage(),
    }
    paths = []
    for func, st_name, ts in specs:
        st = name2storage[st_name]
        path = st.make_path(calls_dir, func)
        st.dump(_mk_entry(func, ts), path)
        paths.append(path)
    return paths


def _write_json_trace(path: Path, func: str, ts: str) -> None:
    """Write a minimal JSON trace file that clean.py can load."""
    data = {
        "trace_schema": "pytead/v1",
        "func": func,
        "args": [1, 2],  # arbitrary but valid
        "kwargs": {"k": "v"},  # arbitrary but valid
        "result": 3,  # arbitrary but valid
        "timestamp": ts,  # e.g. "2025-08-08T10:00:00Z"
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_repr_trace(path: Path, func: str, ts: str) -> None:
    """Write a minimal .repr trace (a Python literal dict)."""
    data = {
        "trace_schema": "pytead/v1",
        "func": func,
        "args": (),  # tuple is fine
        "kwargs": {},
        "result": 0,
        "timestamp": ts,
    }
    path.write_text(repr(data) + "\n", encoding="utf-8")


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()


def test_clean_deletes_all_formats(tmp_path: Path, monkeypatch):
    calls_dir = tmp_path / "call_logs"
    calls_dir.mkdir()

    # 3 fichiers, un par format
    _write_traces(
        calls_dir,
        [
            ("mymodule.multiply", "pickle", "2025-08-08T10:00:00Z"),
            ("mymodule.multiply", "json", "2025-08-08T10:00:00Z"),
            ("mymodule.multiply", "repr", "2025-08-08T10:00:00Z"),
        ],
    )
    assert len(list(calls_dir.glob("*"))) == 3

    _run_cli(monkeypatch, ["pytead", "clean", "-c", str(calls_dir), "-y"])

    assert list(calls_dir.glob("*")) == []


def test_clean_filters_by_func(tmp_path: Path, monkeypatch):
    calls_dir = tmp_path / "logs"
    calls_dir.mkdir()

    _write_traces(
        calls_dir,
        [
            ("pkg.a", "json", "2025-08-08T10:00:00Z"),
            ("pkg.b", "json", "2025-08-08T10:00:00Z"),
            ("pkg.b", "pickle", "2025-08-08T10:00:00Z"),
        ],
    )
    assert len(list(calls_dir.glob("*"))) == 3

    _run_cli(
        monkeypatch, ["pytead", "clean", "-c", str(calls_dir), "--func", "pkg.a", "-y"]
    )

    remaining = sorted(p.suffix for p in calls_dir.glob("*"))
    assert remaining and len(remaining) == 2


def test_clean_before_date(tmp_path: Path, monkeypatch):
    calls_dir = tmp_path / "logs"
    calls_dir.mkdir()
    old_ts = "2025-08-01T00:00:00Z"
    new_ts = "2025-08-09T00:00:00Z"
    _write_traces(
        calls_dir,
        [
            ("pkg.f", "json", old_ts),
            ("pkg.f", "pickle", new_ts),
        ],
    )
    assert len(list(calls_dir.glob("*"))) == 2

    _run_cli(
        monkeypatch,
        ["pytead", "clean", "-c", str(calls_dir), "--before", "2025-08-05", "-y"],
    )

    remaining = [p.suffix for p in calls_dir.glob("*")]
    assert remaining == [".pkl"]


def test_clean_dry_run_keeps_files_and_lists_them(
    tmp_path: Path, capsys, caplog, monkeypatch
):
    """
    Given a logs directory with traces,
    when running clean with --dry-run,
    then:
      - no file is deleted,
      - paths are listed on stdout,
      - a 'Dry-run:' summary is logged (INFO) by logger 'pytead.clean'.
    """
    # Isolate from any project-level default_config.toml
    monkeypatch.chdir(tmp_path)

    calls_dir = tmp_path / "logs"
    calls_dir.mkdir()

    # Create two traces in different formats
    _write_json_trace(calls_dir / "pkg_x__a.json", "pkg.x", "2025-08-08T10:00:00Z")
    _write_repr_trace(calls_dir / "pkg_y__b.repr", "pkg.y", "2025-08-08T10:00:00Z")

    before = sorted(p.name for p in calls_dir.iterdir())
    assert len(before) == 2

    # Capture INFO logs from the clean sub-logger
    caplog.set_level(logging.INFO, logger="pytead.clean")

    # Call the subcommand implementation directly
    args = Namespace(
        calls_dir=calls_dir,
        formats=None,
        functions=[],
        glob=[],
        before=None,
        dry_run=True,
        yes=False,
    )
    clean_run(args)

    # No deletion happened
    after = sorted(p.name for p in calls_dir.iterdir())
    assert after == before

    # Stdout lists the file paths
    out = capsys.readouterr().out
    for name in before:
        assert name in out

    # The summary line is logged (not printed), so assert via caplog
    assert any("Dry-run:" in rec.getMessage() for rec in caplog.records)
