# pytead/gen_tests.py
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import logging
from typing import Any, Dict, List, Union, Optional

from .storage import iter_entries
from ._cases import (
    case_id,
    unique_cases_with_objs,
    render_case_septuple,
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
    Render pytest-compatible test code.

    Parametrization schema per case (7-tuple):
      ('args', 'kwargs', 'expected', 'self_type', 'self_state', 'obj_args', 'result_spec')
    """
    lines: List[str] = []

    # Header with runtime helpers
    lines.append(
        "from pytead.rt import ensure_import_roots, resolve_attr, rehydrate, drop_self_placeholder, inject_object_args, assert_object_state"
    )
    roots = import_roots if import_roots is not None else ["."]
    joined = ", ".join(repr(str(p)) for p in roots)
    lines.append(f"ensure_import_roots(__file__, [{joined}])")
    lines += ["import pytest", ""]

    for func_fullname, entries in sorted(entries_by_func.items(), key=lambda kv: kv[0]):
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"

        cases = unique_cases_with_objs(entries)

        lines.append("@pytest.mark.parametrize(")
        lines.append("    'args, kwargs, expected, self_type, self_state, obj_args, result_spec',")
        lines.append("    [")
        for c in cases:
            lines.extend(render_case_septuple(c, base_indent=8))
        lines.append("    ],")
        lines.append("    ids=[")

        for c in cases:
            lines.append(f"        {case_id(c.args, c.kwargs)!r},")
        lines.append("    ]")
        lines.append(")")
        lines.append(
            f"def test_{module_sanitized}_{func_name}(args, kwargs, expected, self_type, self_state, obj_args, result_spec):"
        )
        lines.append(f"    fq = {func_fullname!r}")
        lines.append("    if self_type:")
        lines.append("        inst = rehydrate(self_type, self_state)")
        lines.append("        method_name = fq.rsplit('.', 1)[1]")
        lines.append("        bound = getattr(inst, method_name)")
        lines.append("        args = drop_self_placeholder(args, self_type)")
        lines.append("        args, kwargs = inject_object_args(args, kwargs, obj_args, self_type)")
        lines.append("        out = bound(*args, **kwargs)")
        lines.append("    else:")
        lines.append("        fn = resolve_attr(fq)")
        lines.append("        args, kwargs = inject_object_args(args, kwargs, obj_args, None)")
        lines.append("        out = fn(*args, **kwargs)")
        lines.append("    if result_spec:")
        lines.append("        typ = resolve_attr(result_spec['type'])")
        lines.append("        assert isinstance(out, typ), f\"expected instance of {result_spec['type']}\"")
        lines.append("        assert_object_state(out, result_spec.get('state') or {})")
        lines.append("    else:")
        lines.append("        assert out == expected")
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

