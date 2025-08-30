from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Iterable,
    Literal,
    Mapping,
    MutableMapping,
    Protocol,
    Sequence,
    TypeGuard,
    TypedDict,
    runtime_checkable,
)

__all__ = [
    "FormatName",
    "SelfSnapshot",
    "BaseTraceEntry",
    "PickleTraceEntry",
    "GraphTraceEntry",
    "TraceEntry",
    "is_graph_entry",
    "is_pickle_entry",
    "coerce_entry_shapes",
    "basic_entry_invariants_ok",
    "StorageLike",
]


# ----------------------------- Format tags -----------------------------------

# Keep this literal tight so mypy catches typos and switch/case exhaustiveness.
FormatName = Literal["pickle", "graph-json"]


# ----------------------------- Trace models ----------------------------------

class SelfSnapshot(TypedDict, total=False):
    """
    Minimal, forward/backward compatible snapshot of `self` around a call.
    Prefer `state_before/state_after`. `before/after` kept for older emitters.
    """
    type: str
    state_before: dict
    state_after: dict
    # Back-compat aliases (older traces may use these)
    before: dict
    after: dict


class BaseTraceEntry(TypedDict, total=False):
    """
    Fields shared by all trace formats. Values should be JSON-serializable
    except for `result` in the pickle format (which can be arbitrary Python).
    """
    trace_schema: str            # e.g., "pytead/anchored-graph" or "pytead/legacy"
    func: str                    # fully-qualified name
    args: tuple                  # always a tuple after coercion
    kwargs: dict                 # always a dict after coercion
    result: Any                  # may be None for graph-json (prefer result_graph)
    timestamp: str               # RFC3339/ISO-8601 if present
    self: SelfSnapshot


class PickleTraceEntry(BaseTraceEntry, total=False):
    """
    Legacy/state-based format: the 'result' is a Python value (pickled).
    Optional object rehydration hints for arguments/results.
    """
    obj_args: dict               # {"pos": {idx: {"type": str, "state": dict}}, "kw": {...}}
    result_obj: dict             # {"type": str, "state": dict}


class GraphTraceEntry(BaseTraceEntry, total=False):
    """
    Anchored graph v2 format. Graph fields are JSON-like structures that may contain:
      - {"$id": int}, {"$ref": int}
      - {"$list": [...]}, {"$tuple": [...]}, {"$set": [...], "$frozen": bool}
      - {"$map": [[k_graph, v_graph], ...]} for non-JSON keys
    Invariants enforced by the writer:
      - result_graph MUST NOT contain orphan {"$ref": N} w.r.t. its OWN anchors.
    """
    args_graph: Any
    kwargs_graph: Any
    result_graph: Any


# Union exposed across the library
TraceEntry = PickleTraceEntry | GraphTraceEntry


# ----------------------------- Type guards -----------------------------------

def is_graph_entry(entry: Mapping[str, Any]) -> TypeGuard[GraphTraceEntry]:
    """
    True if the entry has any of the graph fields used by the anchored v2 format.
    """
    return ("args_graph" in entry) or ("kwargs_graph" in entry) or ("result_graph" in entry)


def is_pickle_entry(entry: Mapping[str, Any]) -> TypeGuard[PickleTraceEntry]:
    """
    Conservative guard for 'pickle'/state-based entries. We assume that if none
    of the graph fields is present, this is a legacy entry.
    """
    return not is_graph_entry(entry)


# ------------------------- Runtime shape helpers -----------------------------

def _as_tuple(x: Any) -> tuple:
    if isinstance(x, tuple):
        return x
    if isinstance(x, Sequence) and not isinstance(x, (str, bytes, bytearray)):
        return tuple(x)
    # Last resort: keep as-is; downstream may still accept it.
    return x  # type: ignore[return-value]


def _as_dict(x: Any) -> dict:
    if isinstance(x, dict):
        return x
    if isinstance(x, Mapping):
        return dict(x)
    return {} if x is None else x  # type: ignore[return-value]


def coerce_entry_shapes(entry: MutableMapping[str, Any]) -> TraceEntry:
    """
    Normalize common shapes in-place (idempotent):
      - ensure args is a tuple,
      - ensure kwargs is a dict (empty if missing/None).
    Returns the same mapping typed as TraceEntry (union).
    """
    if "args" in entry:
        entry["args"] = _as_tuple(entry["args"])
    else:
        entry["args"] = ()
    entry["kwargs"] = _as_dict(entry.get("kwargs", {}))
    # Nothing else is mutated: graph fields and result stay as provided.
    return entry  # type: ignore[return-value]


def basic_entry_invariants_ok(entry: Mapping[str, Any]) -> bool:
    """
    Cheap gate used at the IO boundaries (after load, before generation):
      - 'func' present and is a non-empty string,
      - 'args' is a tuple, 'kwargs' is a dict,
      - graph-json: if present, graph fields are dict/list/scalar (not callables).
    This is intentionally loose; strict validation stays in storage/generation.
    """
    func = entry.get("func")
    if not isinstance(func, str) or not func:
        return False
    if not isinstance(entry.get("args", ()), tuple):
        return False
    if not isinstance(entry.get("kwargs", {}), dict):
        return False

    if is_graph_entry(entry):
        for k in ("args_graph", "kwargs_graph", "result_graph"):
            if k in entry:
                v = entry[k]
                if not isinstance(v, (dict, list, tuple, type(None), int, float, str, bool)):
                    return False
    return True


# ----------------------------- Storage protocol ------------------------------

@runtime_checkable
class StorageLike(Protocol):
    """
    Minimal protocol for a storage backend. Backends are free to keep extra
    invariants internally (e.g., orphan-ref checks) but must expose this API.
    """
    extension: str

    def make_path(self, storage_dir: Path, func_fullname: str) -> Path:
        ...

    def dump(self, entry: Mapping[str, Any], path: Path) -> None:
        """
        Persist a single trace entry to 'path'. The backend may coerce/validate
        the mapping. Implementations should be atomic on write.
        """
        ...

    def load(self, path: Path) -> dict[str, Any]:
        """
        Load and return a raw dict. Callers are encouraged to run
        `coerce_entry_shapes(...)` and `basic_entry_invariants_ok(...)`
        immediately after loading.
        """
        ...

