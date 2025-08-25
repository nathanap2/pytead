# pytead/gen_tests.py

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import logging
import textwrap
import pprint
import uuid
import importlib
import inspect
import typing
import math
import sys
import importlib.util
from types import ModuleType
from typing import Any, Dict, List, Union, Optional, Tuple, Set
from contextlib import contextmanager

from .storage import iter_entries
from ._cases import unique_cases as unique_legacy_cases, render_case

__all__ = ["collect_entries", "render_tests", "write_tests", "write_tests_per_func"]
log = logging.getLogger("pytead.gen")


# ---------------------------------------------------------------------------
# Utilities for robust, refactoring-friendly test generation
# ---------------------------------------------------------------------------

def _sanitize_for_code(obj: Any) -> Any:
    """Make data safe as a Python literal in generated source (NaN/Inf -> None)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (list, tuple)):
        t = [_sanitize_for_code(x) for x in obj]
        return tuple(t) if isinstance(obj, tuple) else t
    if isinstance(obj, dict):
        return {k: _sanitize_for_code(v) for k, v in obj.items()}
    return obj


def _split_owner_and_callable(func_fqn: str) -> tuple[str, Optional[str], str]:
    """
    Split a fully-qualified callable name into (module_path, owner_class|None, func_name).

    Strategy:
      * Walk backward to find the longest importable module prefix.
      * If the direct import fails, try loading the module from its file path via
        `_load_module_from_fqn` (which scans sys.path for <pkg>/<mod>.py).
      * Remaining parts are interpreted as attributes (Class, method/function).

    Examples:
      - "a.b.c"                  -> ("a.b", None, "c")
      - "a.b.C.m"                -> ("a.b", "C", "m")
      - "pkg.mod.Outer.Inner.f"  -> fallback path (not fully supported → returns ("pkg.mod.Outer", None, "Inner"))
    """
    parts = func_fqn.split(".")
    for i in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:i])
        try:
            mod = _load_module_from_fqn(module_path)
            if mod is None:
                raise ImportError(module_path)
            remaining = parts[i:]
            if len(remaining) == 1:
                # module.func
                return module_path, None, remaining[0]
            elif len(remaining) == 2:
                # module.Class.method
                return module_path, remaining[0], remaining[1]
            else:
                # Nested classes not handled here; caller will still succeed because
                # we only need to know if it's a method (class owner present) vs a function.
                break
        except ImportError:
            continue

    # Fallback: treat everything except the last segment as module path
    module_path, attr = func_fqn.rsplit(".", 1)
    return module_path, None, attr


def _collect_types_recursively(cls: Any, all_types: set, processed: set, global_ns: dict):
    """
    Recursively collect dependent types found in the __init__ annotations of a class,
    including parameterized generics from typing (Optional, List[T], etc.).
    """
    if not inspect.isclass(cls) or cls in processed or cls.__module__ == "builtins":
        return

    processed.add(cls)
    all_types.add(cls)

    try:
        type_hints = typing.get_type_hints(cls.__init__, globalns=global_ns)
        for param_type in type_hints.values():
            origin = typing.get_origin(param_type) or param_type
            args = typing.get_args(param_type)
            _collect_types_recursively(origin, all_types, processed, global_ns)
            for arg in args:
                _collect_types_recursively(arg, all_types, processed, global_ns)
    except Exception:
        pass


def _load_module_from_fqn(module_fqn: str) -> Optional[ModuleType]:
    """
    Try to import a module by name first. If it fails, search sys.path for a
    matching <module>.py file and load it via spec_from_file_location.

    This is especially useful when generation happens in a process where only
    the *generated* test file will later bootstrap import_roots. Here, we mimic
    that environment by temporarily adjusting sys.path (see context manager below).
    """
    try:
        importlib.invalidate_caches()
        return importlib.import_module(module_fqn)
    except ImportError:
        log.debug("Direct import of '%s' failed; trying file-based loading.", module_fqn)

    parts = module_fqn.split(".")
    relative_path = Path(*parts).with_suffix(".py")

    for base in sys.path:
        if not base:
            continue
        candidate = Path(base) / relative_path
        if candidate.is_file():
            try:
                spec = importlib.util.spec_from_file_location(module_fqn, candidate)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    # Insert in sys.modules before execution (handles circular imports).
                    sys.modules[module_fqn] = module
                    spec.loader.exec_module(module)
                    log.debug("Loaded '%s' from '%s' via file-based import.", module_fqn, candidate)
                    return module
            except Exception as e:
                log.warning("Error loading %s: %s", candidate, e)
    return None


def _resolve_callable_with_submodules(func_fqn: str) -> Tuple[Any, ModuleType | None, Optional[type]]:
    """
    Resolve a callable object by name, attempting file-based module loading if needed.
    Returns (callable_obj, module, owner_class|None).
    """
    parts = func_fqn.split(".")
    if len(parts) < 2:
        raise ImportError(f"Invalid path: '{func_fqn}'")

    for i in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:i])
        remaining = parts[i:]

        module = _load_module_from_fqn(module_path)
        if module:
            try:
                obj = module
                owner_class = None
                for part in remaining:
                    obj = getattr(obj, part)
                    if inspect.isclass(obj):
                        owner_class = obj
                if obj is owner_class:
                    owner_class = None
                return obj, module, owner_class
            except AttributeError:
                continue

    raise ImportError(f"Could not resolve '{func_fqn}' after several attempts.")


def _get_param_info(func_fqn: str, is_method: bool | None = None) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """
    Inspect a function OR method and return:
      - param_types: {param_name -> annotation type} (excluding 'self'),
      - imports_needed: {module_name -> {ClassName, ...}} for hydration/imports.

    The result helps us generate imports that rehydrate arguments from graphs.
    """
    param_class_map: dict[str, Any] = {}
    imports_needed: Dict[str, Set[str]] = {}

    try:
        fn, mod, owner_cls = _resolve_callable_with_submodules(func_fqn)
    except Exception as e:
        log.warning("Could not introspect function %s to get param types: %s", func_fqn, e)
        return param_class_map, {}

    method_like = False
    try:
        qual = getattr(fn, "__qualname__", "")
        method_like = (owner_cls is not None) or ("." in qual and "(" not in qual)
    except Exception:
        pass
    if is_method is not None:
        method_like = bool(is_method)

    globalns = {}
    try:
        defining_module = inspect.getmodule(fn)
        if defining_module is not None:
            globalns = vars(defining_module)
    except Exception:
        pass

    try:
        type_hints = typing.get_type_hints(fn, globalns=globalns)
    except Exception as e:
        log.debug("get_type_hints failed for %s (%s); falling back to empty", func_fqn, e)
        type_hints = {}

    try:
        sig = inspect.signature(fn)
        param_names = [p.name for p in sig.parameters.values()]
    except Exception:
        param_names = [k for k in type_hints.keys() if k != "return"]

    if method_like and param_names and param_names[0] == "self":
        param_names = param_names[1:]

    from collections import defaultdict
    imports_acc: Dict[str, Set[str]] = defaultdict(set)

    def _record_class_for_import(cls: Any):
        modn = getattr(cls, "__module__", None)
        name = getattr(cls, "__name__", None)
        if modn and name and modn != "builtins":
            imports_acc[modn].add(name)

    def _collect(tp: Any, seen: set):
        if tp is None or tp in seen:
            return
        seen.add(tp)
        origin = typing.get_origin(tp)
        if origin is not None:
            for a in typing.get_args(tp) or ():
                _collect(a, seen)
            return
        if inspect.isclass(tp):
            _record_class_for_import(tp)
            try:
                cls_mod = sys.modules.get(tp.__module__)
                cls_globalns = vars(cls_mod) if cls_mod else {}
                init = getattr(tp, "__init__", None)
                if init and (init is not object.__init__):
                    init_hints = typing.get_type_hints(init, globalns=cls_globalns)
                    for k, v in init_hints.items():
                        if k in ("self", "return"):
                            continue
                        _collect(v, seen)
            except Exception:
                pass
            return
        modn = getattr(tp, "__module__", None)
        name = getattr(tp, "__name__", None)
        if modn and name and modn != "builtins":
            _record_class_for_import(tp)

    seen: set = set()
    for name in param_names:
        ann = type_hints.get(name)
        if ann is None:
            continue
        param_class_map[name] = ann
        _collect(ann, seen)

    imports_needed = {m: set(ns) for m, ns in imports_acc.items()}
    return param_class_map, imports_needed


def render_graph_snapshot_test_body(
    func_name: str,
    entry: dict,
    param_types: dict[str, Any],
    owner_class: Optional[str] = None,
) -> str:
    """
    Render the source of a single pytest test function that performs a
    graph-snapshot check for one recorded call.

    Parameters
    ----------
    func_name : str
        The short function/method name (without module/class).
    entry : dict
        A single trace entry with keys "args_graph", "kwargs_graph", "result_graph".
    param_types : dict[str, Any]
        Mapping from parameter name -> annotation type object (when importable).
        Used to decide whether to rehydrate complex arguments into class instances.
    owner_class : Optional[str]
        If not None, indicates we are targeting a method of `owner_class` (string with class name
        available in the same module import the caller emits).

    Behavior
    --------
    - The graph snapshots (args/kwargs/result) are embedded as Python literals after a
      sanitization pass that converts NaN/±Inf -> None for code-safety.
    - Before calling the target, data graphs are *normalized* back to Python containers
      using `graph_to_data(...)`. For arguments whose param annotation is a non-builtin
      class, we also attempt rehydration using `rehydrate_from_graph(...)`.
    - For methods:
        * If args_graph[0] is present, it is assumed to be the `self` graph and is used
          to build `self_instance`.
        * If no `self` graph is available and the method has no args/kwargs, we fall back
          to a tiny wrapper that constructs `owner_class()` with no arguments and invokes
          the method. (This may fail if the constructor needs args, but it's a best-effort
          fallback consistent with prior behavior.)
    """
    import pprint, uuid

    args_graph = entry.get("args_graph", [])
    kwargs_graph = entry.get("kwargs_graph", {})
    result_graph = entry.get("result_graph", None)

    # Pretty-print sanitized graphs as valid Python literals in the generated file.
    pretty_args = pprint.pformat(_sanitize_for_code(args_graph), indent=4, width=88, sort_dicts=True)
    pretty_kwargs = pprint.pformat(_sanitize_for_code(kwargs_graph), indent=4, width=88, sort_dicts=True)
    pretty_result = pprint.pformat(_sanitize_for_code(result_graph), indent=4, width=88, sort_dicts=True)

    test_name = f"test_{func_name}_snapshot_{uuid.uuid4().hex[:8]}"

    lines_rehydrate: List[str] = []
    owner_offset = 0

    # --- Self handling for methods ----------------------------------------------------
    if owner_class:
        if len(args_graph) >= 1:
            # Rehydrate the instance from the first positional arg graph
            lines_rehydrate.append(
                f"    self_instance = rehydrate_from_graph(graph_to_data(args_graph[0]), {owner_class})"
            )
            owner_offset = 1
        else:
            # No self captured. If there are no other args/kwargs, use a tiny wrapper
            # that calls owner_class().method() with no params.
            if not kwargs_graph:
                wrapper = [
                    f"    def {func_name}():",
                    f"        return {owner_class}().{func_name}()",
                ]
                call_line = f"    real_result = {func_name}()"
                lines = [
                    f"def {test_name}():",
                    "    # 1) Raw graphs embedded from the trace",
                    f"    args_graph = {pretty_args}",
                    f"    kwargs_graph = {pretty_kwargs}",
                    f"    expected_graph = {pretty_result}",
                    "",
                    "    # 2) Zero-arg method wrapper (no `self` captured in trace)",
                    *wrapper,
                    "",
                    "    # 3) Call",
                    call_line,
                    "",
                    "    # 4) Compare snapshot",
                    "    assert_match_graph_snapshot(real_result, expected_graph)",
                ]
                return "\n".join(lines)

    # --- Argument rehydration / normalization ----------------------------------------
    # Figure out how many *effective* positional args (excluding potential self)
    param_names = list(param_types.keys())
    effective_args = max(0, len(args_graph) - owner_offset)
    hydrated_names: List[str] = []

    if param_names and len(param_names) == effective_args:
        # We have names for all positional args → use annotations when available
        for i, name in enumerate(param_names):
            src_idx = i + owner_offset
            var = f"hydrated_arg_{i}"
            hydrated_names.append(var)
            cls = param_types.get(name)
            if isinstance(cls, type) and getattr(cls, "__module__", "") != "builtins":
                # Complex type → normalize graph, then rehydrate into cls
                lines_rehydrate.append(
                    f"    {var} = rehydrate_from_graph(graph_to_data(args_graph[{src_idx}]), {cls.__name__})"
                )
            else:
                # Simple or untyped arg → normalize only
                lines_rehydrate.append(f"    {var} = graph_to_data(args_graph[{src_idx}])")
    else:
        # Positional-only or no annotations → normalize every arg
        for i in range(effective_args):
            src_idx = i + owner_offset
            var = f"hydrated_arg_{i}"
            hydrated_names.append(var)
            lines_rehydrate.append(f"    {var} = graph_to_data(args_graph[{src_idx}])")

    # Always normalize kwargs graph before the call (handles $map/$set encodings).
    if kwargs_graph:
        lines_rehydrate.append("    kwargs_graph = graph_to_data(kwargs_graph)")

    # Build the call signature string
    if hydrated_names and kwargs_graph:
        call_sig = f"{', '.join(hydrated_names)}, **kwargs_graph"
    elif hydrated_names:
        call_sig = ", ".join(hydrated_names)
    elif kwargs_graph:
        call_sig = "**kwargs_graph"
    else:
        call_sig = ""

    # Build the call line (method vs function)
    call_line = (
        f"    real_result = self_instance.{func_name}({call_sig})"
        if owner_class else
        f"    real_result = {func_name}({call_sig})"
    )

    # --- Assemble final test source ---------------------------------------------------
    lines: List[str] = []
    lines.append(f"def {test_name}():")
    lines.append("    # 1) Raw graphs embedded from the trace (sanitized: NaN/Inf -> None)")
    lines.append(f"    args_graph = {pretty_args}")
    lines.append(f"    kwargs_graph = {pretty_kwargs}")
    lines.append(f"    expected_graph = {pretty_result}")
    lines.append("")
    lines.append("    # 2) Normalize/rehydrate arguments")
    lines.extend(lines_rehydrate or ["    pass"])
    lines.append("")
    lines.append("    # 3) Invoke the target")
    lines.append(call_line)
    lines.append("")
    lines.append("    # 4) Compare result graph with the expected snapshot")
    lines.append("    assert_match_graph_snapshot(real_result, expected_graph)")
    return "\n".join(lines)



@contextmanager
def _temporarily_prepend_sys_path(roots: list[str]):
    """
    Temporarily prepend `roots` to sys.path for the duration of a block,
    then restore the original sys.path exactly.

    This is used during generation to make sure import checks (for deciding
    module/class/method splits) see the same roots that the generated tests
    will later bootstrap at runtime.
    """
    old = list(sys.path)
    try:
        for p in reversed(roots or []):
            if p and p not in sys.path:
                sys.path.insert(0, p)
        yield
    finally:
        sys.path[:] = old


def write_tests_per_func(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    output_dir: Union[str, Path],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> None:
    """
    Write one test module per function into `output_dir`.

    For legacy formats, tests are parameterized state-based tests.
    For graph-json, each trace becomes a snapshot test function.

    Robustness tweak:
      We resolve (module, owner_class, func_name) while temporarily prepending
      `import_roots` to sys.path in the generator process (not only embedding
      them in the generated file). This prevents wrong import lines such as
      `from world.BaseEntity import get_coordinates` when the real import
      should be `from world import BaseEntity`.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Normalize and reuse `resolved_roots` for both generator-time imports
    # and for the bootstrap block embedded into the generated file.
    resolved_roots = [str(Path(p).resolve()) for p in (import_roots or []) if p]
    if not resolved_roots:
        resolved_roots = [str(Path.cwd())]

    for func_fullname, entries in sorted(entries_by_func.items()):
        if not entries:
            continue

        sample_trace = entries[0]
        module_sanitized = func_fullname.replace(".", "_")

        if "args_graph" in sample_trace:
            filename = f"test_{module_sanitized}_snapshots.py"

            # Prepare sys.path for the generator process to make module resolution stable.
            with _temporarily_prepend_sys_path(resolved_roots):
                mod_name, owner_cls, func_name = _split_owner_and_callable(func_fullname)
                param_types, imports_needed = _get_param_info(func_fullname)

            # Embed a bootstrap for sys.path so tests run in user environments
            bootstrap_lines = [
                "import sys",
                "from pathlib import Path",
                "",
                "# Bootstrap sys.path to make user code importable",
                f"_IMPORTS_ROOTS = {resolved_roots!r}",
                "for p in _IMPORTS_ROOTS:",
                "    if p not in sys.path:",
                "        sys.path.insert(0, p)",
                "",
            ]

            import_lines = [
                "import pytest",
                "from pytead.testkit import assert_match_graph_snapshot, rehydrate_from_graph, graph_to_data",
            ]
            if owner_cls:
                import_lines.append(f"from {mod_name} import {owner_cls}")
            else:
                import_lines.append(f"from {mod_name} import {func_name}")

            # Additional imports for param types referenced in hydration
            for mod, names in sorted(imports_needed.items()):
                if not mod or mod == "builtins":
                    continue
                line = f"from {mod} import {', '.join(sorted(list(names)))}"
                if line not in import_lines:
                    import_lines.append(line)

            test_functions: List[str] = [
                render_graph_snapshot_test_body(
                    func_name, entry, param_types, owner_class=owner_cls
                )
                for entry in entries
            ]

            source = "\n".join(bootstrap_lines + import_lines) + "\n\n" + "\n\n".join(test_functions) + "\n"
            (out_path / filename).write_text(source, encoding="utf-8")

        else:
            # Legacy: emit a single parameterized module aggregating cases
            filename = f"test_{module_sanitized}.py"
            source = _render_legacy_tests({func_fullname: entries}, import_roots=resolved_roots)
            (out_path / filename).write_text(
                source + ("" if source.endswith("\n") else "\n"),
                encoding="utf-8",
            )


def _render_legacy_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render legacy, state-based tests in a single module (pytest parameterized).
    """
    lines: List[str] = [
        "# Auto-generated by pytead - Legacy format",
        "import pytest",
        "from pytead.testkit import setup as _tk_setup, run_case as _tk_run, param_ids as _tk_ids",
    ]
    roots = import_roots if import_roots is not None else ["."]
    joined_roots = ", ".join(repr(str(p)) for p in roots)
    lines.append(f"_tk_setup(__file__, [{joined_roots}])")
    lines.append("")
    for func_fullname, entries in sorted(entries_by_func.items()):
        cases = unique_legacy_cases(entries)
        if not cases:
            continue
        parts = func_fullname.split(".")
        module_path, func_name = ".".join(parts[:-1]), parts[-1]
        module_sanitized = module_path.replace(".", "_") if module_path else "root"
        cases_variable_name = f"CASES_{module_sanitized}_{func_name}"
        lines.append(f"{cases_variable_name} = [")
        for c in cases:
            lines.extend(render_case(c, base_indent=4))
        lines.append("]")
        lines.append("")
        lines.append(f"@pytest.mark.parametrize('case', {cases_variable_name}, ids=_tk_ids({cases_variable_name}))")
        lines.append(f"def test_{module_sanitized}_{func_name}(case):")
        lines.append(f"    _tk_run({func_fullname!r}, case)")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_entries(
    storage_dir: Union[str, Path], formats: Optional[List[str]] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Group trace entries by function FQN from a calls directory.
    """
    path = Path(storage_dir)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Calls directory '{storage_dir}' does not exist or is not a directory")
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
    Render tests either as legacy (single module) or inform the caller that
    graph-json requires per-function files.
    """
    if not entries_by_func:
        return ""
    sample_trace = next(iter(entries_by_func.values()))[0]
    if "args_graph" in sample_trace:
        return "# Graph snapshot tests are generated one per file. Use --output-dir."
    else:
        return _render_legacy_tests(entries_by_func, import_roots)


def write_tests(source: str, output_file: Union[str, Path]) -> None:
    """
    Write a single test module to disk, creating parent directories if needed.
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source + ("\n" if not source.endswith("\n") else ""), encoding="utf-8")

