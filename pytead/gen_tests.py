# pytead/gen_tests.py
from collections import defaultdict
from pathlib import Path
import logging
from typing import Any, Dict, List, Tuple, Union, Optional

from .storage import iter_entries
from ._cases import unique_cases, case_id, render_case_tuple


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


def render_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render pytest-compatible test code using parameterized tests for each function.

    Key design:
      - We avoid 'from ... import ...' entirely and dynamically resolve the callable
        (function, classmethod, staticmethod, or attribute) from its fully-qualified
        name using importlib — this supports 'module.Class.method' reliably.
      - If 'import_roots' is provided, a header injects runtime sys.path entries
        (absolute or relative to the detected project root).

    Ensures:
      - stable order of functions,
      - unique test function names (module prefix to avoid collisions),
      - pretty-printed literals to avoid ultra-long single lines in editors.
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

    # Dynamic resolver for 'package.module.Class.method' etc.
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
        "import pytest",
        "",
    ]

    for func_fullname, entries in sorted(entries_by_func.items(), key=lambda kv: kv[0]):
        # func_fullname may be 'pkg.mod.fn' or 'pkg.mod.Class.method'
        # We keep it as-is and resolve dynamically in the test body.
        # Use a sanitized name for the pytest function to avoid collisions.
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"

        cases = unique_cases(entries)

        # Parametrized block
        lines.append("@pytest.mark.parametrize(")
        lines.append("    'args, kwargs, expected',")
        lines.append("    [")
        for args_tuple, kwargs_dict, expected_val in cases:
            lines.extend(
                render_case_tuple(args_tuple, kwargs_dict, expected_val, base_indent=8)
            )
        lines.append("    ],")
        lines.append("    ids=[")
        for args_tuple, kwargs_dict, _ in cases:
            lines.append(f"        {case_id(args_tuple, kwargs_dict)!r},")
        lines.append("    ]")
        lines.append(")")
        lines.append(
            f"def test_{module_sanitized}_{func_name}(args, kwargs, expected):"
        )
        lines.append(f"    fn = _resolve_attr({func_fullname!r})")
        lines.append("    assert fn(*args, **kwargs) == expected")
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

