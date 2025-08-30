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
import sys
import importlib.util
from types import ModuleType
from typing import Any, Dict, List, Union, Optional, Tuple, Set
from contextlib import contextmanager

from .storage import iter_entries

from .normalize import sanitize_for_py_literals, tuples_to_lists

from .errors import GenerationError, OrphanRefInExpected
from .graph_utils import find_orphan_refs_in_rendered, inline_and_project_expected
from ._cases import unique_cases, render_case, case_id

from .typing_defs import TraceEntry, is_graph_entry


__all__ = ["collect_entries", "render_tests", "write_tests", "write_tests_per_func"]
log = logging.getLogger("pytead.gen")

def _contains_bare_refs(node) -> bool:
    from .graph_utils import iter_bare_refs_with_paths
    if node is None:
        return False
    try:
        next(iter_bare_refs_with_paths(node))
        return True
    except StopIteration:
        return False

def is_tree_entry(entry: dict) -> bool:
    """
    Heuristique “arbre” : aucun `{"$ref": N}` dans args/kwargs/result.
    """
    return not (
        _contains_bare_refs(entry.get("args_graph")) or
        _contains_bare_refs(entry.get("kwargs_graph")) or
        _contains_bare_refs(entry.get("result_graph"))
    )
    
def render_readable_value_test_body(
    func_name: str,
    entry: dict,
    param_types: dict[str, Any],
    owner_class: Optional[str] = None,
) -> str:
    import uuid

    args_graph = entry.get("args_graph", [])
    kwargs_graph = entry.get("kwargs_graph", {})
    # On garde la même préparation d'expected que pour les snapshots (rendered, sans $id)
    expected_graph = compute_expected_snapshot(entry, func_qualname=func_name)

    # Plan d'invocation (identique aux snapshots, donc robuste pour méthodes/kwargs)
    owner_lines, rehydrate_lines, call_line, wrapper_block = _plan_invocation_and_rehydration(
        func_name=func_name,
        owner_class=owner_class,
        param_types=param_types,
        args_graph=args_graph,
        kwargs_graph=kwargs_graph,
    )

    test_name = f"test_{func_name}_readable_{uuid.uuid4().hex[:8]}"

    lines: list[str] = []
    lines.append(f"def {test_name}():")
    lines.append("    # 1) Graphs embedded (tree -> readable value comparison)")
    lines.append(f"    args_graph = {_fmt_literal_for_embed(args_graph)}")
    lines.append(f"    kwargs_graph = {_fmt_literal_for_embed(kwargs_graph)}")
    lines.append(f"    expected_graph = {_fmt_literal_for_embed(expected_graph)}")
    lines.append("")
    lines.append("    # 2) Normalize/rehydrate arguments")
    lines.append("    from pytead.testkit import graph_to_data, rehydrate_from_graph")
    lines.extend(owner_lines or [])
    lines.extend(rehydrate_lines or ["    pass"])
    lines.append("")
    lines.append("    # 3) Invoke the target")
    lines.append(call_line)
    lines.append("")
    lines.append("    # 4) Compare Python values (mild normalization for literals)")
    lines.append("    from pytead.normalize import sanitize_for_py_literals, tuples_to_lists")
    lines.append("    expected = graph_to_data(expected_graph)")
    lines.append("    def _norm(x):")
    lines.append("        return sanitize_for_py_literals(tuples_to_lists(x))")
    lines.append("    assert _norm(real_result) == _norm(expected)")
    return "\n".join(lines)







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

def _collect_ids_for_refcheck(node, out: set[int]) -> None:
    """
    Collect all integer `$id` anchors contained anywhere under `node`.
    We traverse dicts, lists, and special shapes like {"$map": [...]}, {"$set": [...]}
    without altering anything.
    """
    if node is None:
        return
    if isinstance(node, dict):
        val = node.get("$id")
        if isinstance(val, int):
            out.add(val)
        # special shapes
        if isinstance(node.get("$map"), list):
            for i, pair in enumerate(node["$map"]):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    _collect_ids_for_refcheck(pair[0], out)
                    _collect_ids_for_refcheck(pair[1], out)
            return
        if isinstance(node.get("$set"), list):
            for e in node["$set"]:
                _collect_ids_for_refcheck(e, out)
            return
        # generic dict
        for v in node.values():
            _collect_ids_for_refcheck(v, out)
        return
    if isinstance(node, list):
        for x in node:
            _collect_ids_for_refcheck(x, out)






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






def _fmt_literal_for_embed(obj: Any) -> str:
    """Pretty-prints a value with NaN/±Inf sanitized for safe embedding."""
    return pprint.pformat(sanitize_for_py_literals(obj), indent=4, width=88, sort_dicts=True)



def compute_expected_snapshot(entry: Dict[str, Any], *, func_qualname: str = "") -> Any:
    """
    Thin wrapper that delegates to the unified pipeline, ensuring a single,
    consistent behavior for building the **rendered expected graph** from a trace entry.

    Notes
    -----
    - `tuples_as_lists=True` normalizes tuples to lists in the rendered view
      to keep snapshots JSON-friendly and assertion-stable.
    """
    return inline_and_project_expected(
        entry,
        func_qualname=func_qualname,
        tuples_as_lists=True,
    )

def _plan_invocation_and_rehydration(
    *,
    func_name: str,
    owner_class: Optional[str],
    param_types: dict[str, Any],
    args_graph: Any,
    kwargs_graph: Any,
) -> tuple[list[str], list[str], str, Optional[list[str]]]:
    """
    Retourne (owner_lines, rehydrate_lines, call_line, wrapper_block_or_None).
    - owner_lines: ex. création de self_instance si method avec self capturé.
    - rehydrate_lines: lignes 'hydrated_arg_i = ...' + normalisation kwargs.
    - call_line: ligne d'appel réelle.
    - wrapper_block_or_None: si method sans self et sans kwargs → petit wrapper.
    """
    owner_lines: list[str] = []
    rehydrate_lines: list[str] = []
    wrapper_block: Optional[list[str]] = None
    owner_offset = 0

    # --- owner / self ---------------------------------------------------------
    if owner_class:
        if isinstance(args_graph, list) and len(args_graph) >= 1:
            owner_lines.append(
                f"    self_instance = rehydrate_from_graph(graph_to_data(args_graph[0]), {owner_class})"
            )
            owner_offset = 1
        else:
            # Pas de self capturé et pas de kwargs → wrapper zéro-arg
            if not kwargs_graph:
                wrapper_block = [
                    f"    def {func_name}():",
                    f"        return {owner_class}().{func_name}()",
                ]
                call_line = f"    real_result = {func_name}()"
                return owner_lines, rehydrate_lines, call_line, wrapper_block

    # --- positional args ------------------------------------------------------
    effective_args = max(0, (len(args_graph) if isinstance(args_graph, list) else 0) - owner_offset)
    param_names = list(param_types.keys())
    hydrated_names: list[str] = []

    if param_names and len(param_names) == effective_args:
        for i, name in enumerate(param_names):
            src_idx = i + owner_offset
            var = f"hydrated_arg_{i}"
            hydrated_names.append(var)
            cls = param_types.get(name)
            if isinstance(cls, type) and getattr(cls, "__module__", "") != "builtins":
                rehydrate_lines.append(
                    f"    {var} = rehydrate_from_graph(graph_to_data(args_graph[{src_idx}]), {cls.__name__})"
                )
            else:
                rehydrate_lines.append(f"    {var} = graph_to_data(args_graph[{src_idx}])")
    else:
        for i in range(effective_args):
            src_idx = i + owner_offset
            var = f"hydrated_arg_{i}"
            hydrated_names.append(var)
            rehydrate_lines.append(f"    {var} = graph_to_data(args_graph[{src_idx}])")

    # --- kwargs ---------------------------------------------------------------
    if kwargs_graph:
        rehydrate_lines.append("    kwargs_graph = graph_to_data(kwargs_graph)")

    # --- call signature & line ------------------------------------------------
    if hydrated_names and kwargs_graph:
        call_sig = f"{', '.join(hydrated_names)}, **kwargs_graph"
    elif hydrated_names:
        call_sig = ", ".join(hydrated_names)
    elif kwargs_graph:
        call_sig = "**kwargs_graph"
    else:
        call_sig = ""

    call_line = (
        f"    real_result = self_instance.{func_name}({call_sig})"
        if owner_class else
        f"    real_result = {func_name}({call_sig})"
    )
    return owner_lines, rehydrate_lines, call_line, wrapper_block


# --- fonction principale (découpée) ------------------------------------------

def render_graph_snapshot_test_body(
    func_name: str,
    entry: dict,
    param_types: dict[str, Any],
    owner_class: Optional[str] = None,
) -> str:
    """
    Rend le corps d'un test snapshot (graph-json) pour un appel.
    Découpée en 3 étapes testables : expected, plan d'invocation, embedding.
    """
    import uuid

    args_graph = entry.get("args_graph", [])
    kwargs_graph = entry.get("kwargs_graph", {})
    result_graph = entry.get("result_graph", None)

    # 1) expected prêt à embarquer (avec détection d’orphelines en entrée)
    expected_graph = compute_expected_snapshot(entry, func_qualname=func_name)

    # 2) Plan d’invocation & rehydration
    owner_lines, rehydrate_lines, call_line, wrapper_block = _plan_invocation_and_rehydration(
        func_name=func_name,
        owner_class=owner_class,
        param_types=param_types,
        args_graph=args_graph,
        kwargs_graph=kwargs_graph,
    )

    # 3) Embedding (littéraux jolis & assemblage)
    pretty_args = _fmt_literal_for_embed(args_graph)
    pretty_kwargs = _fmt_literal_for_embed(kwargs_graph)
    pretty_result = _fmt_literal_for_embed(expected_graph)

    test_name = f"test_{func_name}_snapshot_{uuid.uuid4().hex[:8]}"

    if wrapper_block is not None:
        # Cas spécial method sans self capturé (et sans kwargs) — wrapper zéro-arg
        lines = [
            f"def {test_name}():",
            "    # 1) Raw graphs embedded (expected snapshot already inlined/sanitized)",
            f"    args_graph = {pretty_args}",
            f"    kwargs_graph = {pretty_kwargs}",
            f"    expected_graph = {pretty_result}",
            "",
            "    # 2) Zero-arg method wrapper (no `self` captured in trace)",
            *wrapper_block,
            "",
            "    # 3) Call",
            call_line,
            "",
            "    # 4) Compare snapshot",
            "    assert_match_graph_snapshot(real_result, expected_graph)",
        ]
        return "\n".join(lines)

    # Chemin standard (fonction ou method avec self capturé / kwargs)
    lines: list[str] = []
    lines.append(f"def {test_name}():")
    lines.append("    # 1) Raw graphs embedded (expected snapshot already inlined/sanitized)")
    lines.append(f"    args_graph = {pretty_args}")
    lines.append(f"    kwargs_graph = {pretty_kwargs}")
    lines.append(f"    expected_graph = {pretty_result}")
    lines.append("")
    lines.append("    # 2) Normalize/rehydrate arguments")
    lines.extend(owner_lines or [])
    lines.extend(rehydrate_lines or ["    pass"])
    lines.append("")
    lines.append("    # 3) Invoke the target")
    lines.append(call_line)
    lines.append("")
    lines.append("    # 4) Compare result graph with the expected snapshot")
    lines.append("    assert_match_graph_snapshot(real_result, expected_graph)")
    return "\n".join(lines)

def render_state_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render **state-based (pickle)** tests in a single module (pytest parameterized).
    This is the canonical generator for the pickle-backed format.
    """
    lines: List[str] = [
        "# Auto-generated by pytead - state-based (pickle) tests",
        "import pytest",
        "from pytead.testkit import setup as _tk_setup, run_case as _tk_run, param_ids as _tk_ids",
    ]
    roots = import_roots if import_roots is not None else ["."]
    joined_roots = ", ".join(repr(str(p)) for p in roots)
    lines.append(f"_tk_setup(__file__, [{joined_roots}])")
    lines.append("")
    for func_fullname, entries in sorted(entries_by_func.items()):
        cases = unique_cases(entries)
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
        lines.append(
            f"@pytest.mark.parametrize('case', {cases_variable_name}, ids=_tk_ids({cases_variable_name}))"
        )
        lines.append(f"def test_{module_sanitized}_{func_name}(case):")
        lines.append(f"    _tk_run({func_fullname!r}, case)")
        lines.append("")
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
    entries_by_func: Dict[str, List[TraceEntry]],
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

        if is_graph_entry(sample_trace):
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

#            test_functions: List[str] = [
#                render_graph_snapshot_test_body(
#                    func_name, entry, param_types, owner_class=owner_cls
#                )
#                for entry in entries
#            ]
            test_functions: List[str] = []
            for entry in entries:
                if is_tree_entry(entry):
                    test_functions.append(
                        render_readable_value_test_body(
                            func_name, entry, param_types, owner_class=owner_cls
                        )
                    )
                else:
                    test_functions.append(
                        render_graph_snapshot_test_body(
                            func_name, entry, param_types, owner_class=owner_cls
                        )
                    )

            source = "\n".join(bootstrap_lines + import_lines) + "\n\n" + "\n\n".join(test_functions) + "\n"
            (out_path / filename).write_text(source, encoding="utf-8")
            
        else:
            # State-based (pickle): single parameterized module (one function)
            filename = f"test_{module_sanitized}.py"
            source = render_state_tests({func_fullname: entries}, import_roots=resolved_roots)
            (out_path / filename).write_text(
                source + ("" if source.endswith("\n") else "\n"),
                encoding="utf-8",
            )

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_entries(
    storage_dir: Union[str, Path], formats: Optional[List[str]] = None
) -> Dict[str, List[TraceEntry]]:
    """
    Group trace entries by function FQN from a calls directory.
    """
    path = Path(storage_dir)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Calls directory '{storage_dir}' does not exist or is not a directory")
    entries_by_func: Dict[str, List[TraceEntry]] = defaultdict(list)
    for entry in iter_entries(path, formats=formats):
        func = entry.get("func")
        if not func:
            log.warning("Skipping trace without 'func'")
            continue
        entries_by_func[func].append(entry)
    return dict(entries_by_func)



def render_tests(
    entries_by_func: Dict[str, List[TraceEntry]],
    import_roots: Optional[List[Union[str, Path]]] = None,
) -> str:
    """
    Render a single Python test module from a collection of trace entries.

    Behavior:
    - For state-based ("pickle") traces, return a full pytest module that aggregates
      all functions using parameterized tests (via `render_state_tests`), bootstrapping
      the provided `import_roots`.
    - For graph-based ("graph-json") traces, this helper does not inline tests into a
      single file. Graph snapshot tests are generated one file per target for clarity
      and isolation (see `write_tests_per_func`). In that case, a short sentinel
      source string is returned.
    - If the mapping is empty or contains only empty lists, return an empty string.
    """
    if not entries_by_func:
        return ""

    # Find the first non-empty entry list, if any.
    sample_trace: Optional[TraceEntry] = None
    for entries in entries_by_func.values():
        if entries:
            sample_trace = entries[0]
            break
    if sample_trace is None:
        return ""

    # Decide rendering strategy based on the sample's format.
    if is_graph_entry(sample_trace):
        return "# Graph snapshot tests are generated one per file. Use --output-dir."

    # State-based (pickle): aggregate into a single module.
    return render_state_tests(entries_by_func, import_roots=import_roots or [])


def write_tests(source: str, output_file: Union[str, Path]) -> None:
    """
    Write a single test module to disk, creating parent directories if needed.
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source + ("\n" if not source.endswith("\n") else ""), encoding="utf-8")

