# pytead/gen_tests.py
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import logging
from typing import Any, Dict, List, Tuple, Union, Optional
import textwrap

from .storage import iter_entries
from ._cases import unique_cases, case_id, pformat  # keep existing helpers for IDs/formatting


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

    # Return a plain dict
    return dict(entries_by_func)


# ---------------------------------------------------------------------------
# Extended normalization/deduplication including self snapshot + object args/result
# We move from 5-tuple (args, kwargs, expected, self_type, self_state)
# to 7-tuple (args, kwargs, expected, self_type, self_state, obj_args, result_spec).
# ---------------------------------------------------------------------------

def _normalize_with_objs(
    e: Dict[str, Any]
) -> Tuple[tuple, dict, Any, Optional[str], Optional[dict], Optional[dict], Optional[dict]]:
    """
    Extract a stable, hashable-friendly view of one trace entry:
      - args: tuple
      - kwargs: dict
      - expected: any
      - self_type: str | None
      - self_state: dict | None  (we keep 'state_before' for rehydration)
      - obj_args: dict | None    (see tracing.py schema: {"pos":{idx:spec}, "kw":{name:spec}})
      - result_spec: dict | None (spec for returned object, if any)
    """
    args = tuple(e.get("args", ()))
    kwargs = dict(e.get("kwargs", {}) or {})
    expected = e.get("result")

    s = e.get("self") or {}
    self_type = s.get("type")
    self_state = s.get("state_before")

    obj_args = e.get("obj_args") if isinstance(e.get("obj_args"), dict) else None
    result_spec = e.get("result_obj") if isinstance(e.get("result_obj"), dict) else None

    return args, kwargs, expected, self_type, self_state, obj_args, result_spec


def _case_key_with_objs(
    args: tuple,
    kwargs: dict,
    expected: Any,
    self_type: Optional[str],
    self_state: Optional[dict],
    obj_args: Optional[dict],
    result_spec: Optional[dict],
) -> str:
    """Stable key for deduplication; repr-based is sufficient for our literal-friendly payload."""
    try:
        kw_items = tuple(sorted(kwargs.items()))
    except Exception:
        kw_items = tuple(kwargs.items())
    return repr((args, kw_items, expected, self_type, self_state, obj_args, result_spec))


def _unique_cases_with_objs(
    entries: List[Dict[str, Any]]
) -> List[Tuple[tuple, dict, Any, Optional[str], Optional[dict], Optional[dict], Optional[dict]]]:
    """Deduplicate while taking into account self snapshot + object inputs/outputs."""
    seen, out = set(), []
    for e in entries:
        tup = _normalize_with_objs(e)
        k = _case_key_with_objs(*tup)
        if k in seen:
            continue
        seen.add(k)
        out.append(tup)
    return out


def _render_case_septuple(
    args: tuple,
    kwargs: dict,
    expected: Any,
    self_type: Optional[str],
    self_state: Optional[dict],
    obj_args: Optional[dict],
    result_spec: Optional[dict],
    base_indent: int = 8,
) -> List[str]:
    """
    Pretty-print a single 7-tuple case for the parametrize block (with indentation).
    """
    indent_item = " " * base_indent
    indent_body = " " * (base_indent + 4)
    body = (
        f"{pformat(args)},\n"
        f"{pformat(kwargs)},\n"
        f"{pformat(expected)},\n"
        f"{pformat(self_type)},\n"
        f"{pformat(self_state)},\n"
        f"{pformat(obj_args)},\n"
        f"{pformat(result_spec)},"
    )
    return [f"{indent_item}(", textwrap.indent(body, indent_body), f"{indent_item}),"]


# ---------------------------------------------------------------------------
# Test rendering
# ---------------------------------------------------------------------------

def render_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render pytest-compatible test code.

    Design:
      - Lightweight header importing runtime helpers from pytead.rt:
            ensure_import_roots, resolve_attr, rehydrate, drop_self_placeholder,
            inject_object_args, assert_object_state
      - Optional sys.path setup via ensure_import_roots(__file__, [...])
      - Instance methods are replayed by rehydrating 'self' from snapshots.
      - Simple objects in inputs are reconstructed via inject_object_args(...).
      - Simple object outputs are validated structurally (attributes), not by identity.

    Parametrization schema per case (7 tuple):
      ('args', 'kwargs', 'expected', 'self_type', 'self_state', 'obj_args', 'result_spec')

    Notes:
      - For instance methods, if traces stored a 'self' placeholder string as args[0]
        (JSON/REPR formats), the test drops it before calling the bound method.
      - For pickle traces, args never contain the bound arg; nothing is dropped.
    """
    lines: List[str] = []

    # --- compact, readable header using pytead.rt helpers ---
    lines.append(
        "from pytead.rt import ensure_import_roots, resolve_attr, rehydrate, drop_self_placeholder, inject_object_args, assert_object_state"
    )
    roots = import_roots if import_roots is not None else ["."]
    joined = ", ".join(repr(str(p)) for p in roots)
    lines.append(f"ensure_import_roots(__file__, [{joined}])")
    lines += [
        "import pytest",
        "",
    ]
    # --------------------------------------------------------

    for func_fullname, entries in sorted(entries_by_func.items(), key=lambda kv: kv[0]):
        # Split for a readable test name; resolution stays dynamic on the full FQN.
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"

        cases = _unique_cases_with_objs(entries)

        # Parametrized block with self + obj_args + result_spec
        lines.append("@pytest.mark.parametrize(")
        lines.append("    'args, kwargs, expected, self_type, self_state, obj_args, result_spec',")
        lines.append("    [")
        for a, k, r, stype, sstate, oas, rspec in cases:
            lines.extend(
                _render_case_septuple(a, k, r, stype, sstate, oas, rspec, base_indent=8)
            )
        lines.append("    ],")
        lines.append("    ids=[")
        for a, k, _r, _t, _s, _oa, _rs in cases:
            lines.append(f"        {case_id(a, k)!r},")
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
        lines.append("        # Validate the structure of the returned object (not identity).")
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

