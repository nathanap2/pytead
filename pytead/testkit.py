# pytead/testkit.py
from __future__ import annotations

from typing import Iterable, Tuple, Any, List, Union, Optional, Sequence
from os import PathLike
import inspect
import math

from ._cases import case_id as _case_id
from .rt import (
    ensure_import_roots, resolve_attr, rehydrate,
    drop_self_placeholder, inject_object_args, assert_object_state,
)
from .graph_capture import capture_object_graph


# ---------------------------------------------------------------------------
# Public helpers exported by the testkit
# ---------------------------------------------------------------------------

__all__ = [
    "setup",
    "run_case",
    "param_ids",
    "assert_match_graph_snapshot",
    "is_literal_like",
    "graph_to_data",
    "sanitize_for_py_literals",
    "rehydrate_from_graph",
]

# Type alias for legacy/state-based case tuples
Case = Tuple[
    tuple,              # args
    dict,               # kwargs
    Any,                # expected (or None if result_spec is used)
    Optional[str],      # self_type ("pkg.Mod.Class") if method, else None
    Optional[dict],     # self_state (full/private snapshot)
    Optional[dict],     # obj_args (rehydration spec for non-literals)
    Optional[dict],     # result_spec (type+state for returned object)
]


# ---------------------------------------------------------------------------
# Graph-snapshot assertions and utilities
# ---------------------------------------------------------------------------

def sanitize_for_py_literals(obj: Any) -> Any:
    """
    Runtime counterpart of the generator-side sanitizer:
    replace float NaN/±Inf with None, recursively, so comparisons and Python
    literals remain stable across platforms.

    This function is *idempotent* and safe to apply on already-sanitized data.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (list, tuple)):
        t = [sanitize_for_py_literals(x) for x in obj]
        return tuple(t) if isinstance(obj, tuple) else t
    if isinstance(obj, dict):
        return {k: sanitize_for_py_literals(v) for k, v in obj.items()}
    return obj
    
def _tuples_to_lists(obj: Any) -> Any:
    """Recursively convert tuples to lists so JSON-ish graphs compare equal."""
    if isinstance(obj, tuple):
        return [_tuples_to_lists(x) for x in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj


def graph_to_data(node: Any) -> Any:
    """
    Convert a captured object *graph* (dicts/lists with markers) back to plain
    Python containers and scalars.

    Supported markers:
      - {"$map": [[k_graph, v_graph], ...]}  -> dict{graph_to_data(k): graph_to_data(v)},
        with robust key rehydration (lists -> tuples, sets -> frozenset, "$ref" -> ("$ref", n), etc.)
      - {"$set": [...], "$frozen": bool}     -> set(...) or frozenset(...), fallback to list if unhashable
      - {"$ref": N}                          -> left as-is (we don't rebuild aliasing)

    Any other dict/list is traversed recursively. Scalars are returned as-is.
    """

    def _keyify(k_graph: Any) -> Any:
        """
        Rehydrate a *key* graph into a hashable Python object.
        - lists -> tuples (so tuple keys round-trip),
        - {"$set": ...} -> frozenset(...) (or tuple(...) as a last resort),
        - {"$ref": n} -> ("$ref", n) placeholder (hashable),
        - {"$map": ...} -> tuple of pairs (keyified_key, graph_to_data(value)) sorted by repr,
        - plain dict -> frozenset of (keyified_key, graph_to_data(value)) pairs (fallback to tuple).
        - scalars -> graph_to_data(scalar)
        """
        if isinstance(k_graph, list):
            return tuple(_keyify(x) for x in k_graph)

        if isinstance(k_graph, dict):
            if "$map" in k_graph and isinstance(k_graph["$map"], list):
                pairs = [(_keyify(sk), graph_to_data(sv)) for (sk, sv) in k_graph["$map"]]
                try:
                    # Sort for determinism; keys are hashable thanks to _keyify.
                    return tuple(sorted(pairs, key=lambda kv: repr(kv[0])))
                except Exception:
                    return tuple(pairs)

            if "$set" in k_graph:
                elems = [graph_to_data(x) for x in k_graph.get("$set", [])]
                try:
                    return frozenset(elems)
                except TypeError:
                    return tuple(elems)

            if "$ref" in k_graph:
                # Hashable placeholder for a reference we cannot resolve here.
                return ("$ref", k_graph["$ref"])

            # Plain dict: freeze its items
            items = [(_keyify(kk), graph_to_data(vv)) for kk, vv in k_graph.items()]
            try:
                return frozenset(items)
            except TypeError:
                # Fall back to a deterministic tuple
                return tuple(sorted(items, key=lambda kv: repr(kv[0])))

        # Scalar / already hashable path
        return graph_to_data(k_graph)

    if isinstance(node, dict):
        # Heterogeneous / non-string dict keys
        if "$map" in node and isinstance(node["$map"], list):
            pairs = []
            for k, v in node["$map"]:
                hk = _keyify(k)
                vv = graph_to_data(v)
                pairs.append((hk, vv))
            try:
                return {k: v for (k, v) in pairs}
            except TypeError:
                # If some keys remain unhashable despite _keyify, degrade to a list of pairs
                return pairs

        # Set / frozenset
        if "$set" in node:
            elems = [graph_to_data(x) for x in node.get("$set", [])]
            try:
                return frozenset(elems) if node.get("$frozen") else set(elems)
            except TypeError:
                # Fallback (e.g., contains dicts/"$ref"): keep as list
                return elems

        # Reference marker — keep as-is (matching capture semantics)
        if "$ref" in node:
            return {"$ref": node["$ref"]}

        # Plain mapping
        return {k: graph_to_data(v) for k, v in node.items()}

    if isinstance(node, list):
        return [graph_to_data(x) for x in node]

    return node

def assert_match_graph_snapshot(
    real_result: Any,
    expected_graph: dict,
    max_depth: int = 5
) -> None:
    """
    Capture the *data graph* of a real result and compare it to an expected graph
    (as recorded in traces).

    To accommodate code generation that replaces NaN/±Inf by None in literals,
    we sanitize *both* sides before comparison. This keeps tests deterministic
    while avoiding invalid Python literals (e.g., `nan`) in generated files.
    """
    real_result_graph = capture_object_graph(real_result, max_depth=max_depth)
    # 1) numeric sanitization (NaN/Inf -> None)
    real_result_graph = sanitize_for_py_literals(real_result_graph)
    expected_graph = sanitize_for_py_literals(expected_graph)
    # 2) structural normalization (tuples -> lists) to match JSON encoding
    real_result_graph = _tuples_to_lists(real_result_graph)
    expected_graph = _tuples_to_lists(expected_graph)

    assert real_result_graph == expected_graph, "The object graph does not match the snapshot."


def is_literal_like(x: Any) -> bool:
    """
    Check whether an object is composed only of Python literal-friendly types:
    (None, bool, int, float, str, list/tuple of literals, dict with str keys and literal values).
    """
    if x is None or isinstance(x, (bool, int, float, str)):
        return True
    if isinstance(x, (list, tuple)):
        return all(is_literal_like(e) for e in x)
    if isinstance(x, dict):
        return all(isinstance(k, str) and is_literal_like(v) for k, v in x.items())
    return False


def rehydrate_from_graph(graph_data: Any, target_class: type) -> Any:
    """
    Rehydrate a *data graph* into an instance of `target_class`.

    Steps:
      1) Normalize `graph_data` with `graph_to_data` so `$map`/`$set` encodings
         are turned back into plain Python containers.
      2) Inspect `target_class.__init__` and build kwargs by name; when a field
         is itself a dict and the parameter is annotated, recursively rehydrate
         with the annotation type. Otherwise, pass the value as-is.

    Notes:
      - This is a heuristic suitable for test replays; it doesn't attempt to
        invoke arbitrary complex constructors or rebuild object identity graphs.
    """
    # 1) Normalize the structure first (handles non-string keys, sets, etc.)
    graph_data = graph_to_data(graph_data)

    # Lists of graphs -> list of rehydrated objects
    if isinstance(graph_data, list):
        return [rehydrate_from_graph(item, target_class) for item in graph_data]

    # Primitive or non-class targets -> pass through
    if not isinstance(graph_data, dict) or not inspect.isclass(target_class):
        return graph_data

    # 2) Build kwargs smartly from the constructor signature
    try:
        sig = inspect.signature(target_class.__init__)
        params = sig.parameters
    except (TypeError, ValueError):
        # Some builtins or C-extensions might not expose a signature
        return graph_data

    init_args = {}
    for name, param in params.items():
        if name == "self" or name not in graph_data:
            continue

        arg_value = graph_data[name]

        # If annotation exists and arg is a dict, try to rehydrate recursively
        if (
            param.annotation is not inspect.Parameter.empty
            and isinstance(arg_value, dict)
            and inspect.isclass(param.annotation)
        ):
            init_args[name] = rehydrate_from_graph(arg_value, param.annotation)
        else:
            init_args[name] = arg_value

    return target_class(**init_args)


# ---------------------------------------------------------------------------
# Legacy/state-based test runtime
# ---------------------------------------------------------------------------

def setup(here_file: Union[str, PathLike[str]], import_roots: Iterable[Union[str, PathLike[str]]]) -> None:
    """
    Prepare sys.path for generated tests. Relative paths are anchored on the
    project root (auto-detected around `here_file`).
    """
    ensure_import_roots(here_file, import_roots)


def run_case(func_fq: str, case: Case) -> None:
    """
    Replay one recorded *legacy* case and assert on result/object state.

    Case schema (7-tuple):
      (args, kwargs, expected, self_type, self_state, obj_args, result_spec)
    - If `self_type` is present, we rehydrate an instance and call the bound method.
    - If `obj_args` provides type/state for arguments, we rehydrate those too.
    - If `result_spec` is present, we assert the returned object type/state;
      otherwise we compare the result value directly to `expected`.
    """
    args, kwargs, expected, self_type, self_state, obj_args, result_spec = case

    if self_type:
        # Instance method path
        inst = rehydrate(self_type, self_state)
        method_name = func_fq.rsplit(".", 1)[1]
        bound = getattr(inst, method_name)
        args = drop_self_placeholder(args, self_type)
        args, kwargs = inject_object_args(args, kwargs, obj_args, self_type)
        out = bound(*args, **kwargs)
    else:
        # Module-level function path
        fn = resolve_attr(func_fq)
        args, kwargs = inject_object_args(args, kwargs, obj_args, None)
        out = fn(*args, **kwargs)

    if result_spec:
        typ = resolve_attr(result_spec["type"])
        assert isinstance(out, typ), f"expected instance of {result_spec['type']}"
        assert_object_state(out, result_spec.get("state") or {})
    else:
        assert out == expected


def param_ids(cases: Sequence[Case], maxlen: int = 80) -> List[str]:
    """
    Generate readable IDs for pytest.parametrize from a sequence of legacy cases.
    """
    ids: List[str] = []
    for args, kwargs, *_ in cases:
        ids.append(_case_id(args, kwargs, maxlen=maxlen))
    return ids

