# tests/test_graphjson_method_import_regression.py
from __future__ import annotations

from pathlib import Path
import textwrap
import pytest


def _w(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")


def test_graphjson_generates_correct_import_for_methods_with_sys_path_append(tmp_path: Path):
    project = tmp_path / "proj"
    calls_dir = tmp_path / "calls"
    out_dir = tmp_path / "out"
    project.mkdir()

    _w(
        project / "world.py",
        """
        class BaseEntity:
            def __init__(self):  # no-arg init pour éviter toute ambiguïté
                self.x = 3
                self.y = 5

            def get_coordinates(self):
                return {"x": self.x, "y": self.y}
        """
    )

    script = project / "app.py"
    _w(
        script,
        f"""
        import sys
        sys.path.append({repr(str(project))})
        from world import BaseEntity

        if __name__ == "__main__":
            BaseEntity().get_coordinates()
        """
    )

    from pytead.cli import service_cli as svc

    instr, outcome, _roots = svc.instrument_and_run(
        targets=["world.BaseEntity.get_coordinates"],
        limit=5,
        storage_dir=calls_dir,
        storage="graph-json",
        script_file=script,
        argv=[str(script)],
    )
    assert outcome.status.name in {"OK", "SYSTEM_EXIT"}, outcome

    res = svc.collect_and_emit_tests(
        storage_dir=calls_dir,
        formats=None,
        output=None,
        output_dir=out_dir,
        import_roots=[str(project)],
    )
    assert res is not None and res.files_written >= 1

    snapshot_files = list(out_dir.glob("test_*_snapshots.py"))
    assert snapshot_files, "No snapshot test module was generated."
    src = snapshot_files[0].read_text(encoding="utf-8")

    # ✅ Après patch, on attend la bonne forme d'import pour une méthode :
    assert "from world import BaseEntity" in src
    assert "from world.BaseEntity import get_coordinates" not in src

