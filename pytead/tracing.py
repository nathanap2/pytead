# pytead/tracing.py
from __future__ import annotations

import functools
import inspect
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union, cast
import re

from .storage import PickleStorage, _to_literal as _to_literal

# Package-level logger stays quiet unless the host/CLI configures it.
_pkg_logger = logging.getLogger("pytead")
if not any(isinstance(h, logging.NullHandler) for h in _pkg_logger.handlers):
    _pkg_logger.addHandler(logging.NullHandler())
_logger = logging.getLogger("pytead.tracing")


# ---------------------- Formatting helpers (unchanged behavior) ----------------------

_OPAQUE_REPR_RE = re.compile(r"^<[\w\.]+ object at 0x[0-9A-Fa-f]+>$")


def _safe_repr_or_classname(x: Any) -> str:
    """
    Prefer a meaningful repr; if it looks like the default '<Pkg.Class object at 0x...>',
    fall back to the fully-qualified class name.
    """
    try:
        r = repr(x)
    except Exception:
        r = None
    if not r:
        t = type(x)
        name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
        return f"{t.__module__}.{name}" if t.__module__ and t.__module__ != "builtins" else name
    if _OPAQUE_REPR_RE.match(r):
        t = type(x)
        name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
        return f"{t.__module__}.{name}" if t.__module__ and t.__module__ != "builtins" else name
    return r


def _stringify_level1(value: Any) -> Any:
    """
    Depth=1 stringify: turn non-builtin objects into strings (repr-or-classname).
    For builtin containers, apply the same conversion to their direct elements,
    without recursing deeper.
    """
    # Scalars / bytes-likes / None → keep literal-friendly form via _to_literal
    if value is None or isinstance(
        value, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice)
    ):
        return _to_literal(value)

    # Builtin containers: map only one level
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kk = _to_literal(k)  # keys should stay literal
            vv = (
                _safe_repr_or_classname(v)
                if not isinstance(
                    v,
                    (
                        bool,
                        int,
                        float,
                        complex,
                        str,
                        bytes,
                        bytearray,
                        memoryview,
                        range,
                        slice,
                        list,
                        tuple,
                        set,
                        frozenset,
                        dict,
                    ),
                )
                else _to_literal(v)
            )
            # one-level container mapping for direct elements
            if isinstance(v, (list, tuple, set, frozenset)):
                vv = [
                    _safe_repr_or_classname(e)
                    if not isinstance(
                        e,
                        (
                            bool,
                            int,
                            float,
                            complex,
                            str,
                            bytes,
                            bytearray,
                            memoryview,
                            range,
                            slice,
                            list,
                            tuple,
                            set,
                            frozenset,
                            dict,
                        ),
                    )
                    else _to_literal(e)
                    for e in v
                ]
                if isinstance(v, tuple):
                    vv = tuple(vv)
                if isinstance(v, (set, frozenset)):
                    vv = sorted(vv)  # determinism
            elif isinstance(v, dict):
                vv = {
                    _to_literal(kk2): (
                        _safe_repr_or_classname(vv2)
                        if not isinstance(
                            vv2,
                            (
                                bool,
                                int,
                                float,
                                complex,
                                str,
                                bytes,
                                bytearray,
                                memoryview,
                                range,
                                slice,
                                list,
                                tuple,
                                set,
                                frozenset,
                                dict,
                            ),
                        )
                        else _to_literal(vv2)
                    )
                    for kk2, vv2 in v.items()
                }
            out[kk] = vv
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        seq = [
            _safe_repr_or_classname(e)
            if not isinstance(
                e,
                (
                    bool,
                    int,
                    float,
                    complex,
                    str,
                    bytes,
                    bytearray,
                    memoryview,
                    range,
                    slice,
                    list,
                    tuple,
                    set,
                    frozenset,
                    dict,
                ),
            )
            else _to_literal(e)
            for e in value
        ]
        if isinstance(value, (set, frozenset)):
            seq = sorted(seq)  # determinism
        return tuple(seq) if isinstance(value, tuple) else list(seq)

    # Any other (probably user-defined) object → string
    return _safe_repr_or_classname(value)


def _qualtype(obj: Any) -> str:
    """Return a readable fully-qualified type name for an instance."""
    t = type(obj)
    name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
    return f"{t.__module__}.{name}" if t.__module__ and t.__module__ != "builtins" else name


def _iter_slots(cls: type) -> list[str]:
    """Collect __slots__ names along the MRO (handles str or iterable forms)."""
    names: list[str] = []
    try:
        for c in cls.mro():
            s = getattr(c, "__slots__", ())
            if not s:
                continue
            if isinstance(s, str):
                names.append(s)
            else:
                names.extend(list(s))
    except Exception:
        # Be conservative; snapshotting must never break user code.
        pass
    return names


def _snapshot_object(obj: Any, include_private: bool = False) -> Dict[str, Any]:
    """
    Shallow snapshot of an object's state as {attr_name: literal_value}.

    - Uses __dict__ when available.
    - Completes with __slots__ when present.
    - Skips callables and descriptors.
    - Best-effort: any failure on an attribute is ignored.
    - Values are converted to literal-ish forms via _to_literal.
    """
    snap: Dict[str, Any] = {}

    # __dict__
    try:
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            for k, v in d.items():
                if not include_private and str(k).startswith("_"):
                    continue
                try:
                    if callable(v):
                        continue
                    snap[k] = _to_literal(v)
                except Exception:
                    # Never raise from snapshotting.
                    pass
    except Exception:
        pass

    # __slots__
    try:
        for name in _iter_slots(type(obj)):
            if not include_private and str(name).startswith("_"):
                continue
            if name in snap:
                continue
            try:
                v = getattr(obj, name)
            except Exception:
                continue
            try:
                if callable(v):
                    continue
                snap[name] = _to_literal(v)
            except Exception:
                pass
    except Exception:
        pass

    return snap


# ---------------------- New: extracted trace core helpers ----------------------

@dataclass(frozen=True)
class _TracePolicy:
    limit: int
    storage_dir: Path
    storage: Any
    capture_objects: str                 # "off" | "simple"
    include_private_objects: bool
    objects_stringify_depth: int


def _is_builtin_like(x: Any) -> bool:
    """
    True if `x` is a literal-safe scalar or a builtin container (we don't snapshot it).
    """
    if x is None or isinstance(x, (str, bytes, bytearray, memoryview, bool, int, float, complex)):
        return True
    if isinstance(x, (range, slice)):
        return True
    if isinstance(x, (list, tuple, set, frozenset, dict)):
        return True
    return False


def _obj_spec(x: Any, include_private: bool, stringify_depth: int) -> Optional[dict]:
    """
    Non-builtin objects → {"type": fqname, "state": {...}}.
    depth == 0  : canonical snapshot via _snapshot_object
    depth >= 1  : enumerate __dict__/__slots__ and stringify one level
    """
    if getattr(type(x), "__module__", "") == "builtins":
        return None

    t = _qualtype(x)

    if stringify_depth <= 0:
        try:
            state0 = _snapshot_object(x, include_private=include_private)
        except Exception:
            state0 = {}
        return {"type": t, "state": state0}

    state: dict[str, Any] = {}
    try:
        processed: set[str] = set()

        # __dict__
        d = getattr(x, "__dict__", None)
        if isinstance(d, dict):
            for k, v in d.items():
                if not include_private and str(k).startswith("_"):
                    continue
                try:
                    if callable(v):
                        continue
                    state[str(k)] = _stringify_level1(v)
                    processed.add(str(k))
                except Exception:
                    pass

        # __slots__
        for name in _iter_slots(type(x)):
            if not include_private and str(name).startswith("_"):
                continue
            if str(name) in processed:
                continue
            try:
                v = getattr(x, name)
            except Exception:
                continue
            try:
                if callable(v):
                    continue
                state[str(name)] = _stringify_level1(v)
            except Exception:
                pass

    except Exception:
        state = {}

    return {"type": t, "state": state}


def _maybe_snapshot_self_before(args: tuple, snapshot_self: bool) -> Tuple[Optional[str], Optional[dict], Optional[dict]]:
    """Return (self_type, pub_before, all_before) or (None, None, None)."""
    if not (snapshot_self and len(args) >= 1):
        return None, None, None
    try:
        inst = args[0]
        self_type = _qualtype(inst)
        pub_before = _snapshot_object(inst, include_private=False)
        all_before = _snapshot_object(inst, include_private=True)
        return self_type, pub_before, all_before
    except Exception:
        return None, None, None


def _snapshot_self_after(args: tuple, had_pub_before: bool) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (pub_after, all_after) or (None, None)."""
    if not (had_pub_before and len(args) >= 1):
        return None, None
    try:
        pub_after = _snapshot_object(args[0], include_private=False)
        all_after = _snapshot_object(args[0], include_private=True)
        return pub_after, all_after
    except Exception:
        return None, None


def _stored_args_for(st: Any, drop_first: bool, args: tuple) -> tuple:
    """Persisted-args policy depending on storage format (Pickle vs JSON/REPR)."""
    if drop_first and len(args) >= 1:
        if isinstance(st, PickleStorage):
            return args[1:]
        # JSON/REPR: keep a literal-friendly placeholder
        return (repr(args[0]),) + args[1:]
    return args


def _build_obj_captures(args: tuple, kwargs: dict, drop_first: bool, policy: _TracePolicy) -> Tuple[Dict[int, dict], Dict[str, dict]]:
    """Build obj_args.pos/kw maps according to the capture policy."""
    obj_args_pos: Dict[int, dict] = {}
    obj_args_kw: Dict[str, dict] = {}
    if policy.capture_objects == "off":
        return obj_args_pos, obj_args_kw

    for idx, val in enumerate(args):
        if drop_first and idx == 0:
            continue
        if not _is_builtin_like(val):
            spec = _obj_spec(val, policy.include_private_objects, policy.objects_stringify_depth)
            if spec:
                obj_args_pos[idx] = spec

    for k, v in (kwargs or {}).items():
        if not _is_builtin_like(v):
            spec = _obj_spec(v, policy.include_private_objects, policy.objects_stringify_depth)
            if spec:
                obj_args_kw[str(k)] = spec

    return obj_args_pos, obj_args_kw


def _result_obj_spec_for(result: Any, policy: _TracePolicy) -> Optional[dict]:
    if policy.capture_objects == "off" or _is_builtin_like(result):
        return None
    return _obj_spec(result, policy.include_private_objects, policy.objects_stringify_depth)


def _emit_entry(
    st: Any,
    storage_path: Path,
    fullname: str,
    prefix: str,
    *,
    stored_args: tuple,
    kwargs: dict,
    result: Any,
    self_payload: Optional[dict],
    obj_args_pos: Dict[int, dict],
    obj_args_kw: Dict[str, dict],
    result_obj: Optional[dict],
) -> None:
    entry: Dict[str, Any] = {
        "trace_schema": "pytead/v1",
        "func": fullname,
        "args": stored_args,
        "kwargs": kwargs,
        "result": result,
        "timestamp": datetime.utcnow().isoformat(timespec="microseconds") + "Z",
    }
    if self_payload is not None:
        entry["self"] = self_payload
    if obj_args_pos or obj_args_kw:
        entry["obj_args"] = {"pos": obj_args_pos, "kw": obj_args_kw}
    if result_obj is not None:
        entry["result_obj"] = result_obj

    path = st.make_path(storage_path, fullname)
    st.dump(entry, path)


# ---------------------- Public decorator (refactored) ----------------------

def trace(
    limit: int = 10,
    storage_dir: Union[str, Path] = Path("call_logs"),
    storage=None,
    *,
    capture_objects: str = "off",
    include_private_objects: bool = False,
    objects_stringify_depth: int = 0,
):
    """
    Decorator that logs a callable's *root* calls so tests can be generated later.

    Behavior
    --------
    - Per-thread depth tracking ensures only the outermost call is recorded.
    - Pluggable Storage backend (pickle by default).
    - Uses a lightweight, versioned schema header ("pytead/v1").

    Instance methods
    ----------------
    - If the first parameter is named 'self', we:
        * snapshot instance state (public + full/private variants) before/after,
        * store it under entry["self"] = {
              "type": str, "before": dict, "after": dict,
              "state_before": dict, "state_after": dict
          }
        * apply a "bound first arg policy":
            - For **Pickle** storage, drop the bound first arg (`self`/`cls`) from
              stored `args` to avoid pickling local classes/instances.
            - For **JSON/REPR** storages, replace the bound first arg with a **string
              placeholder** (`repr(self_or_cls)`) so the entry remains literal-friendly.
              At replay, tests can drop this placeholder (see pytead.rt.drop_self_placeholder).

    Optional capture of simple objects (inputs/outputs)
    ---------------------------------------------------
        capture_objects: "off" | "simple"
            - "off"   : current behavior (no extra capture for inputs/outputs)
            - "simple": shallow snapshot of non-builtin objects in args/kwargs/result
        include_private_objects: bool
            - include private attributes (starting with '_') when snapshotting inputs/outputs
        objects_stringify_depth: int
            - 0 → canonical literal snapshot; ≥1 → stringify one level inside values
    """
    storage_path = Path(storage_dir)
    st = storage or PickleStorage()
    policy = _TracePolicy(
        limit=limit,
        storage_dir=storage_path,
        storage=st,
        capture_objects=capture_objects,
        include_private_objects=include_private_objects,
        objects_stringify_depth=objects_stringify_depth,
    )

    def _build_wrapper(fn: Callable[..., Any]) -> Callable[..., Any]:
        qual = getattr(fn, "__qualname__", getattr(fn, "__name__", "<lambda>"))
        fullname = f"{fn.__module__}.{qual}"
        prefix = fullname.replace(".", "_")

        # Inspect parameters once
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            has_pos0 = len(params) >= 1 and params[0].kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            first_name = params[0].name if has_pos0 else None
        except Exception:
            has_pos0, first_name = False, None

        # Policy
        drop_first = has_pos0 and first_name in ("self", "cls")
        snapshot_self = has_pos0 and first_name == "self"

        # Depth control (per-thread)
        local = threading.local()

        # Per-decorated callable counter (avoid FS scans on each call)
        counter_lock = threading.Lock()
        initialized = False
        written = 0

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            prev_depth = getattr(local, "depth", 0)
            setattr(local, "depth", prev_depth + 1)
            is_root = prev_depth == 0

            # BEFORE snapshots (public view + full view for rehydration)
            self_type = None
            pub_before = all_before = None
            if is_root:
                self_type, pub_before, all_before = _maybe_snapshot_self_before(args, snapshot_self)

            try:
                result = fn(*args, **kwargs)

                if is_root:
                    # AFTER snapshots for self (if relevant)
                    pub_after = all_after = None
                    if pub_before is not None:
                        pub_after, all_after = _snapshot_self_after(args, had_pub_before=True)

                    nonlocal initialized, written
                    with counter_lock:
                        if not initialized:
                            existing = list(policy.storage_dir.glob(f"{prefix}__*{st.extension}"))
                            written = len(existing)
                            initialized = True

                        if written < policy.limit:
                            stored_args = _stored_args_for(st, drop_first, args)
                            pos_map, kw_map = _build_obj_captures(args, kwargs, drop_first, policy)
                            result_obj = _result_obj_spec_for(result, policy)

                            self_payload = None
                            if pub_before is not None:
                                self_payload = {
                                    "type": self_type,
                                    "before": pub_before,
                                    "after": pub_after,
                                    "state_before": all_before,
                                    "state_after": all_after,
                                }

                            _emit_entry(
                                st,
                                policy.storage_dir,
                                fullname,
                                prefix,
                                stored_args=stored_args,
                                kwargs=kwargs,
                                result=result,
                                self_payload=self_payload,
                                obj_args_pos=pos_map,
                                obj_args_kw=kw_map,
                                result_obj=result_obj,
                            )
                            written += 1

                return result
            finally:
                try:
                    new_depth = getattr(local, "depth", 1) - 1
                    if new_depth <= 0:
                        if hasattr(local, "depth"):
                            delattr(local, "depth")
                    else:
                        setattr(local, "depth", new_depth)
                except Exception:
                    # Cleanup must never crash the caller
                    pass

        return cast(Callable[..., Any], wrapper)

    def decorator(func: Any) -> Any:
        # If the user put @trace *outside* @staticmethod/@classmethod,
        # handle descriptor objects by unwrapping/rewrapping.
        if isinstance(func, staticmethod):
            inner = func.__func__
            return staticmethod(_build_wrapper(inner))
        if isinstance(func, classmethod):
            inner = func.__func__
            return classmethod(_build_wrapper(inner))
        # Normal function (module-level or defined in class body)
        return _build_wrapper(func)

    return decorator

