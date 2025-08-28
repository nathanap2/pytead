from __future__ import annotations
from pathlib import Path
import sys

from pytead.gen_tests import write_tests_per_func

def _w(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")

def test_write_per_func_produces_compilable_file(tmp_path: Path):
    # 1) mini module importable
    pkg = tmp_path / "pkg"; pkg.mkdir()
    mod = pkg / "mod.py"
    _w(mod, """
def build(d):
    # juste pour avoir un callable réel
    return {"done": True, "src": d}
""")
    sys.path.insert(0, str(tmp_path))

    # 2) entrée graph-json: expected réfère à une ancre fournie par args_graph
    func_fqn = "pkg.mod.build"
    entries_by_func = {
        func_fqn: [{
            "func": func_fqn,
            "args_graph": [{"$id": 41, "data": [1, 2]}],
            "kwargs_graph": {},
            "result_graph": {"from_arg": {"$ref": 41}, "ok": True},
        }]
    }

    # 3) génération dans un répertoire
    out_dir = tmp_path / "generated"
    write_tests_per_func(entries_by_func, out_dir, import_roots=[tmp_path])

    # 4) un seul fichier généré -> doit se compiler
    files = list(out_dir.glob("test_*_snapshots.py"))
    assert files, "aucun fichier de test généré"
    src = files[0].read_text(encoding="utf-8")
    compile(src, str(files[0]), "exec")

