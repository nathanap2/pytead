# tests/test_gen_header_imports.py
from __future__ import annotations
from pathlib import Path
import argparse
import subprocess
import sys
import textwrap
import re

import pytead.gen_tests as gen


def _w(p: Path, s: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")
    return p


def _run_pytest_in(cwd: Path) -> subprocess.CompletedProcess:
    # Lance pytest dans un sous-processus isolé
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _assert_passed(stdout: str, expected: int) -> None:
    """
    Extrait 'N passed' de la sortie pytest et compare à expected.
    Accepte des sorties enrichies (warnings, skipped, etc.).
    """
    m = re.search(r"(\d+)\s+passed", stdout)
    if not m:
        raise AssertionError(f"'passed' count not found in pytest output:\n{stdout}")
    got = int(m.group(1))
    assert (
        got == expected
    ), f"expected {expected} passed, got {got}\n\nSTDOUT:\n{stdout}"


def test_render_tests_header_allows_import_outside_root(tmp_path: Path):
    root = tmp_path

    # Marque la racine pour le header (_find_root cherche .pytead/ ou pyproject.toml)
    _w(root / "pyproject.toml", "[build-system]\nrequires=[]\nbuild-backend=''\n")

    # Module sous "src/app/utils.py" (hors racine)
    _w(root / "src/app/__init__.py", "")
    _w(
        root / "src/app/utils.py",
        """
        def add(a, b):
            return a + b
        """,
    )

    # Traces synthétiques = 2 cas → 2 tests paramétrés
    entries = {
        "app.utils.add": [
            {"func": "app.utils.add", "args": (2, 3), "kwargs": {}, "result": 5},
            {"func": "app.utils.add", "args": (10, 0), "kwargs": {}, "result": 10},
        ]
    }

    # Génère un test avec import_roots = [".", "src"]
    source = gen.render_tests(entries, import_roots=[".", "src"])
    _w(root / "tests/generated/test_add.py", source)

    res = _run_pytest_in(root)
    if res.returncode != 0:
        raise AssertionError(
            f"pytest failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    _assert_passed(res.stdout, expected=2)


def test_cmd_gen_generates_tests_with_header_and_they_run(tmp_path: Path, monkeypatch):
    from pytead.cli.cmd_gen import _handle as gen_handle

    root = tmp_path

    # Racine détectable
    _w(root / "pyproject.toml", "[build-system]\nrequires=[]\nbuild-backend=''\n")

    # Code à tester sous "src/app/utils.py"
    _w(root / "src/app/__init__.py", "")
    _w(
        root / "src/app/utils.py",
        """
        def mul(a, b):
            return a * b
        """,
    )

    _w(
        root / ".pytead/config.toml",
        """
        [gen]
        storage_dir = "calls"
        output_dir = "tests/gen"
        additional_sys_path = ["src"]
        """,
    )

    # Traces JSON minimalistes dans calls_dir (2 cas attendus)
    _w(
        root / "calls/app_utils_mul__1.json",
        """
        {
          "trace_schema": "pytead/v1",
          "func": "app.utils.mul",
          "args": [2, 3],
          "kwargs": {},
          "result": 6,
          "timestamp": "2025-01-01T00:00:00Z"
        }
        """,
    )
    _w(
        root / "calls/app_utils_mul__2.json",
        """
        {
          "trace_schema": "pytead/v1",
          "func": "app.utils.mul",
          "args": [10, 0],
          "kwargs": {},
          "result": 0,
          "timestamp": "2025-01-01T00:00:01Z"
        }
        """,
    )

    # Appelle le handler "gen" (CWD = racine du projet)
    monkeypatch.chdir(root)
    ns = argparse.Namespace()  # pas d'options CLI → rempli par la config
    gen_handle(ns)

    # Vérifie qu'au moins un test a été généré
    generated = list((root / "tests/gen").glob("test_app_utils_mul.py"))
    assert generated, "no generated test found"

    # Lance pytest et vérifie le bon nombre de cas (2)
    res = _run_pytest_in(root)
    if res.returncode != 0:
        raise AssertionError(
            f"pytest failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    _assert_passed(res.stdout, expected=2)
