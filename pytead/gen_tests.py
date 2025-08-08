from collections import defaultdict
from pathlib import Path
import pickle
from typing import Any, Dict, List, Union


def collect_entries(calls_dir: Union[str, Path]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Collect all log entries from pickled traces in calls_dir.

    :param calls_dir: Directory containing .pkl trace files.
    :return: A dict mapping function fullname to list of entries.
    """
    path = Path(calls_dir)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Calls directory '{calls_dir}' does not exist or is not a directory")

    entries_by_func: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for log_path in sorted(path.glob("*.pkl")):
        with log_path.open("rb") as f:
            entry = pickle.load(f)
        func = entry.get("func")
        if func:
            entries_by_func[func].append(entry)
    return entries_by_func


def render_tests(entries_by_func: Dict[str, List[Dict[str, Any]]]) -> str:
    """
    Render pytest-compatible test code using parameterized tests for each function.
    """
    lines: List[str] = ["import pytest", ""]
    for func_fullname, entries in entries_by_func.items():
        module_path, func_name = func_fullname.rsplit('.', 1)
        lines.append(f"from {module_path} import {func_name}")
        lines.append("")

        # Deduplicate calls
        seen = set()
        unique_cases = []
        for entry in entries:
            args = tuple(entry.get("args", ()))
            kwargs = entry.get("kwargs", {})
            expected = entry.get("result")
            key = repr((args, kwargs, expected))
            if key not in seen:
                seen.add(key)
                unique_cases.append((args, kwargs, expected))

        # Parameterized test
        lines.append("@pytest.mark.parametrize('args, kwargs, expected', [")
        for args_tuple, kwargs_dict, expected_val in unique_cases:
            lines.append(f"    ({args_tuple!r}, {kwargs_dict!r}, {expected_val!r}),")
        lines.append("])")
        lines.append(f"def test_{func_name}(args, kwargs, expected):")
        lines.append(f"    assert {func_name}(*args, **kwargs) == expected")
        lines.append("")
    return "\n".join(lines)


def write_tests(source: str, output_file: Union[str, Path]) -> None:
    """
    Write the rendered test source to the given output file, creating parent dirs as needed.
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source, encoding="utf-8")


def write_tests_per_func(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    output_dir: Union[str, Path]
) -> None:
    """
    Write rendered test modules per function in the specified output directory.

    :param entries_by_func: Dict mapping function fullname to list of entries.
    :param output_dir: Directory where to write individual test files.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    for func_fullname, entries in entries_by_func.items():
        module_path, func_name = func_fullname.rsplit('.', 1)
        # sanitize module path for filename
        module_sanitized = module_path.replace('.', '_')
        filename = f"test_{module_sanitized}_{func_name}.py"
        source = render_tests({func_fullname: entries})
        (out_path / filename).write_text(source, encoding='utf-8')

