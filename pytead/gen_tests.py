# pytead/gen_tests.py
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import logging
from typing import Any, Dict, List, Union, Optional

from .storage import iter_entries
from ._cases import (
    unique_cases,
    render_case,
)


def collect_entries(
    storage_dir: Union[str, Path], formats: Optional[List[str]] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collect all log entries from traces in storage_dir (supports multiple formats).
    :param formats: optional list like ["pickle", "json"]; default: scan all.
    """
    path = Path(storage_dir)
    if not path.exists() or not path.is_dir():
        raise ValueError(
            f"Calls directory '{storage_dir}' does not exist or is not a directory"
        )

    log = logging.getLogger("pytead.gen")
    entries_by_func: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for entry in iter_entries(path, formats=formats):
        func = entry.get("func")
        if not func:
            log.warning("Skipping trace without 'func'")
            continue
        entries_by_func[func].append(entry)

    return dict(entries_by_func)


def render_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render pytest-compatible test code, déléguant toute l'exécution
    à pytead.testkit (setup/sys.path, replay et assertions).
    """
    lines: List[str] = []

    # En-tête minimal : on utilise le testkit.
    lines.append("from pytead.testkit import setup as _tk_setup, run_case as _tk_run, param_ids as _tk_ids")
    roots = import_roots if import_roots is not None else ["."]
    joined = ", ".join(repr(str(p)) for p in roots)
    lines.append(f"_tk_setup(__file__, [{joined}])")
    lines += ["import pytest", ""]

    for func_fullname, entries in sorted(entries_by_func.items(), key=lambda kv: kv[0]):
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"
        sym_cases = f"CASES_{module_sanitized}_{func_name}"

        cases = unique_cases(entries)

        # Déclaration unique des cas (7-tuple) pour cette fonction
        lines.append(f"{sym_cases} = [")
        for c in cases:
            lines.extend(render_case(c, base_indent=4))
        lines.append("]")
        lines.append("")

        # Paramétrage compact : un seul paramètre 'case'
        lines.append(f"@pytest.mark.parametrize('case', {sym_cases}, ids=_tk_ids({sym_cases}))")
        lines.append(f"def test_{module_sanitized}_{func_name}(case):")
        lines.append(f"    _tk_run({func_fullname!r}, case)")
        lines.append("")

    return "\n".join(lines)


def write_tests(source: str, output_file: Union[str, Path]) -> None:
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        source + ("\n" if not source.endswith("\n") else ""), encoding="utf-8"
    )


def write_tests_per_func(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    output_dir: Union[str, Path],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> None:
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    for func_fullname, entries in sorted(entries_by_func.items(), key=lambda kv: kv[0]):
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"
        filename = f"test_{module_sanitized}_{func_name}.py"
        source = render_tests({func_fullname: entries}, import_roots=import_roots)
        (out_path / filename).write_text(source + "\n", encoding="utf-8")

