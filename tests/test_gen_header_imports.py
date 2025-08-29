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
    assert got == expected, f"expected {expected} passed, got {got}\n\nSTDOUT:\n{stdout}"



import argparse
from pathlib import Path

from pytead.storage import GraphJsonStorage
from pytead.cli.cmd_gen import _handle as gen_handle

# helpers supposés déjà présents dans ton fichier de tests :
# _w(path: Path, text: str) -> None
# _run_pytest_in(root: Path) -> CompletedProcess
# _assert_passed(pytest_stdout: str, expected: int) -> None

def test_cmd_gen_generates_tests_with_header_and_they_run__graphjson(tmp_path: Path, monkeypatch):
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

    # Config CLI pour gen (lecture des traces + sortie par fonction)
    _w(
        root / ".pytead/config.toml",
        """
        [gen]
        storage_dir = "calls"
        output_dir = "tests/gen"
        additional_sys_path = ["src"]
        """,
    )

    # Traces GRAPH-JSON minimalistes dans calls_dir (2 cas attendus)
    calls_dir = root / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    gst = GraphJsonStorage()
    gst.dump(
        {
            "trace_schema": "pytead/v2-graph",
            "func": "app.utils.mul",
            "args_graph": [2, 3],
            "kwargs_graph": {},
            "result_graph": 6,
            "timestamp": "2025-01-01T00:00:00Z",
        },
        calls_dir / "app_utils_mul__1.gjson",
    )
    gst.dump(
        {
            "trace_schema": "pytead/v2-graph",
            "func": "app.utils.mul",
            "args_graph": [10, 0],
            "kwargs_graph": {},
            "result_graph": 0,
            "timestamp": "2025-01-01T00:00:01Z",
        },
        calls_dir / "app_utils_mul__2.gjson",
    )

    # Appelle le handler "gen" (CWD = racine du projet)
    monkeypatch.chdir(root)
    ns = argparse.Namespace()  # pas d’options CLI → rempli par la config [gen]
    gen_handle(ns)

    # Vérifie qu’au moins un test a été généré
    # (pour graph-json: un fichier *par fonction*, suffixé "_snapshots")
    generated = list((root / "tests/gen").glob("test_app_utils_mul_snapshots.py"))
    assert generated, "no generated test found (expected test_app_utils_mul_snapshots.py)"

    # Lance pytest et vérifie le bon nombre de cas (2)
    res = _run_pytest_in(root)
    if res.returncode != 0:
        raise AssertionError(
            f"pytest failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    _assert_passed(res.stdout, expected=2)

