# pytead/gen_tests.py
from collections import defaultdict
from pathlib import Path
import logging
from typing import Any, Dict, List, Tuple, Union, Optional
import textwrap

from .storage import iter_entries
from ._cases import unique_cases, case_id, render_case_tuple, pformat


def collect_entries(
    calls_dir: Union[str, Path], formats: Optional[List[str]] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collect all log entries from traces in calls_dir (supports multiple formats).
    :param formats: optional list like ["pickle", "json"]; default: scan all.
    """
    path = Path(calls_dir)
    if not path.exists() or not path.is_dir():
        raise ValueError(
            f"Calls directory '{calls_dir}' does not exist or is not a directory"
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


# ------------------------
# Helpers for parametrization with 'self'
# ------------------------

def _normalize_with_self(e: Dict[str, Any]) -> Tuple[tuple, dict, Any, Optional[str], Optional[dict]]:
    """Return (args, kwargs, expected, self_type, self_state_before)."""
    args = tuple(e.get("args", ()))
    kwargs = dict(e.get("kwargs", {}) or {})
    expected = e.get("result")

    s = e.get("self")
    if isinstance(s, dict):
        self_type = s.get("type")
        self_state = s.get("state_before")
    else:
        self_type = None
        self_state = None

    return args, kwargs, expected, self_type, self_state


def _case_key_with_self(
    args: tuple, kwargs: dict, expected: Any, self_type: Optional[str], self_state: Optional[dict]
) -> str:
    """Stable key including self snapshot to avoid dropping necessary state."""
    try:
        kw_items = tuple(sorted(kwargs.items()))
    except Exception:
        kw_items = tuple(kwargs.items())
    return repr((args, kw_items, expected, self_type, self_state))


def _unique_cases_with_self(entries: List[Dict[str, Any]]) -> List[Tuple[tuple, dict, Any, Optional[str], Optional[dict]]]:
    """Deduplicate taking into account the self snapshot (if any)."""
    seen, out = set(), []
    for e in entries:
        args, kwargs, expected, self_type, self_state = _normalize_with_self(e)
        k = _case_key_with_self(args, kwargs, expected, self_type, self_state)
        if k in seen:
            continue
        seen.add(k)
        out.append((args, kwargs, expected, self_type, self_state))
    return out


def _render_case_quintuple(
    args: tuple, kwargs: dict, expected: Any, self_type: Optional[str], self_state: Optional[dict], base_indent: int = 8
) -> List[str]:
    """Pretty-print a 5-tuple case for the parametrize block."""
    indent_item = " " * base_indent
    indent_body = " " * (base_indent + 4)
    body = (
        f"{pformat(args)},\n"
        f"{pformat(kwargs)},\n"
        f"{pformat(expected)},\n"
        f"{pformat(self_type)},\n"
        f"{pformat(self_state)},"
    )
    return [f"{indent_item}(", textwrap.indent(body, indent_body), f"{indent_item}),"]


# ------------------------
# Test rendering
# ------------------------

def render_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render pytest-compatible test code.

    Design:
      - Optional header to inject sys.path entries at runtime (relative to detected project root).
      - Dynamic resolution of the callable from its fully-qualified name (supports module.Class.method).
      - Instance methods are replayed by rehydrating 'self' (object.__new__ + state_before).

    Parametrization schema per case:
      ('args', 'kwargs', 'expected', 'self_type', 'self_state')

    Notes:
      - For instance methods, if traces stored a 'self' placeholder string as args[0] (JSON/REPR),
        the test drops it before calling the bound method on the rehydrated instance.
      - For pickle traces, args never contain the bound arg; nothing is dropped.
    """
    lines: List[str] = []

    # Optional: runtime sys.path setup for non-root modules
    if import_roots:
        lines += [
            "import sys, os",
            "from pathlib import Path",
            "",
            "_HERE = Path(__file__).resolve()",
            "def _find_root(start: Path) -> Path:",
            "    for p in [start] + list(start.parents):",
            "        if (p / '.pytead').exists() or (p / 'pyproject.toml').exists():",
            "            return p",
            "    return start.parent",
            "",
            "_ROOT = _find_root(_HERE)",
            "__PYTEAD_IMPORTS = [",
        ]
        for p in import_roots:
            lines.append(f"    {str(p)!r},")
        lines += [
            "]",
            "for _raw in __PYTEAD_IMPORTS:",
            "    _p = _raw if os.path.isabs(_raw) else str((_ROOT / _raw).resolve())",
            "    if _p not in sys.path:",
            "        sys.path.insert(0, _p)",
            "",
        ]

    # Dynamic resolver and rehydrator
    lines += [
        "import importlib",
        "",
        "def _resolve_attr(fq: str):",
        "    parts = fq.split('.')",
        "    # Import the longest importable module prefix",
        "    for i in range(len(parts), 0, -1):",
        "        mod_name = '.'.join(parts[:i])",
        "        try:",
        "            obj = importlib.import_module(mod_name)",
        "            rest = parts[i:]",
        "            break",
        "        except Exception:",
        "            continue",
        "    else:",
        "        raise ImportError(f'Cannot import any prefix of {fq!r}')",
        "    for name in rest:",
        "        obj = getattr(obj, name)",
        "    return obj",
        "",
        "def _rehydrate(type_fq: str, state: dict | None):",
        "    mod_name, cls_name = type_fq.rsplit('.', 1)",
        "    cls = getattr(importlib.import_module(mod_name), cls_name)",
        "    inst = object.__new__(cls)  # bypass __init__",
        "    for k, v in (state or {}).items():",
        "        try:",
        "            object.__setattr__(inst, k, v)",
        "        except Exception:",
        "            try:",
        "                setattr(inst, k, v)",
        "            except Exception:",
        "                pass",
        "    return inst",
        "",
        "import pytest",
        "",
    ]

    for func_fullname, entries in sorted(entries_by_func.items(), key=lambda kv: kv[0]):
        # Split for a readable test name; resolution stays dynamic on the full FQN.
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"

        cases = _unique_cases_with_self(entries)

        # Parametrized block with self info
        lines.append("@pytest.mark.parametrize(")
        lines.append("    'args, kwargs, expected, self_type, self_state',")
        lines.append("    [")
        for a, k, r, stype, sstate in cases:
            lines.extend(_render_case_quintuple(a, k, r, stype, sstate, base_indent=8))
        lines.append("    ],")
        lines.append("    ids=[")
        for a, k, _r, _t, _s in cases:
            lines.append(f"        {case_id(a, k)!r},")
        lines.append("    ]")
        lines.append(")")
        lines.append(
            f"def test_{module_sanitized}_{func_name}(args, kwargs, expected, self_type, self_state):"
        )
        # Branch: instance method vs others
        lines.append(f"    fq = {func_fullname!r}")
        lines.append("    if self_type:")
        lines.append("        # Instance method: rehydrate and bind")
        lines.append("        inst = _rehydrate(self_type, self_state)")
        lines.append("        method_name = fq.rsplit('.', 1)[1]")
        lines.append("        bound = getattr(inst, method_name)")
        lines.append("        # Some storage formats include a 'self' repr as args[0] — drop it if detected")
        lines.append("        if args and isinstance(args[0], str):")
        lines.append("            cls_name = self_type.rsplit('.', 1)[1]")
        lines.append("            if args[0].startswith('<') and cls_name in args[0]:")
        lines.append("                args = args[1:]")
        lines.append("        assert bound(*args, **kwargs) == expected")
        lines.append("    else:")
        lines.append("        # Function / staticmethod / classmethod")
        lines.append("        fn = _resolve_attr(fq)")
        lines.append("        assert fn(*args, **kwargs) == expected")
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

