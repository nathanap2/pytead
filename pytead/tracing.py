# pytead/tracing.py
from __future__ import annotations

import functools
import inspect
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union, cast
import re
from .storage import PickleStorage, _to_literal as _to_literal

# Package-level logger stays quiet unless the host/CLI configures it.
_pkg_logger = logging.getLogger("pytead")
if not any(isinstance(h, logging.NullHandler) for h in _pkg_logger.handlers):
    _pkg_logger.addHandler(logging.NullHandler())
_logger = logging.getLogger("pytead.tracing")


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
    if value is None or isinstance(value, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice)):
        return _to_literal(value)

    # Builtin containers: map only one level
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kk = _to_literal(k)  # keys should stay literal
            vv = (
                _safe_repr_or_classname(v)
                if not isinstance(v, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice, list, tuple, set, frozenset, dict))
                else _to_literal(v)
            )
            # one-level container mapping for direct elements
            if isinstance(v, (list, tuple, set, frozenset)):
                vv = [
                    _safe_repr_or_classname(e)
                    if not isinstance(e, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice, list, tuple, set, frozenset, dict))
                    else _to_literal(e)
                ]
                if isinstance(v, tuple):
                    vv = tuple(vv)
                if isinstance(v, (set, frozenset)):
                    vv = sorted(vv)  # determinism
            elif isinstance(v, dict):
                vv = { _to_literal(kk2): (_safe_repr_or_classname(vv2)
                      if not isinstance(vv2, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice, list, tuple, set, frozenset, dict))
                      else _to_literal(vv2))
                      for kk2, vv2 in v.items() }
            out[kk] = vv
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        seq = [
            _safe_repr_or_classname(e)
            if not isinstance(e, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice, list, tuple, set, frozenset, dict))
            else _to_literal(e)
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
    return (
        f"{t.__module__}.{name}"
        if t.__module__ and t.__module__ != "builtins"
        else name
    )


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
    - Values are converted to literal-ish forms via _to_literal (strings, numbers,
      lists/tuples/dicts of literal-friendly content; otherwise repr).
    """
    snap: Dict[str, Any] = {}

    # __dict__ (most Python objects)
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

    # __slots__ (without overwriting existing keys)
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


def trace(
    limit: int = 10,
    storage_dir: Union[str, Path] = Path("call_logs"),
    storage=None,
    *,
    capture_objects: str = "off",
    include_private_objects: bool = False,
    objects_stringify_depth: int = 0,  # NEW
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
    Controlled via:
        capture_objects: "off" | "simple"
            - "off"   : current behavior (no extra capture for inputs/outputs)
            - "simple": shallow snapshot of non-builtin objects in args/kwargs/result
        include_private_objects: bool
            - include private attributes (starting with '_') when snapshotting inputs/outputs

    When enabled, we add:
        entry["obj_args"] = {
            "pos": { index: {"type": "pkg.Cls", "state": {...}}, ... },
            "kw":  { name:  {"type": "pkg.Cls", "state": {...}}, ... }
        }
        entry["result_obj"] = {"type": "pkg.Cls", "state": {...}}  # if result is a non-builtin object

    Notes:
    - Position indices for "pos" are those of the *call site*, before any drop of `self`.
      The test helper (rt.inject_object_args) will compensate if a self placeholder was present.
    """
    storage_path = Path(storage_dir)
    st = storage or PickleStorage()

    # ---- Local helpers (kept inside to avoid exposing internal policy) ----

    def _is_builtin_like(x: Any) -> bool:
        """
        Return True if x is 'safe-literal' or a builtin container likely represented
        faithfully by repr/json; custom instances return False and are candidates
        for object snapshotting.
        """
        from collections.abc import Mapping, Sequence, Set

        if x is None or isinstance(x, (str, bytes, bytearray, memoryview, bool, int, float, complex)):
            return True
        if isinstance(x, (range, slice)):
            return True
        # Builtin containers (their *elements* may still be objects, but we snapshot the
        # top-level object only in this mode to keep policy simple).
        if isinstance(x, (list, tuple, set, frozenset, dict)):
            return True
        # For anything else (likely a user-defined class instance), return False.
        return False

    def _obj_spec(x: Any, include_private: bool, stringify_depth: int) -> Optional[dict]:
        """
        Produce {"type": fqname, "state": snapshot} or None if x is builtin-like.
        When stringify_depth==1, the state flattens nested objects to strings (repr-or-classname).
        """
        try:
            if getattr(type(x), "__module__", "") == "builtins":
                return None
            t = _qualtype(x)
            if stringify_depth >= 1:
                # Take the canonical snapshot (dict + slots, déjà filtré des callables),
                # then stringify each value at depth=1 for stability + littéralité.
                base = _snapshot_object(x, include_private=include_private)
                state = {k: _stringify_level1(v) for k, v in base.items()}
            else:
                state = _snapshot_object(x, include_private=include_private)
            return {"type": t, "state": state}
        except Exception:
            return None

    # ----------------------------------------------------------------------

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
            pub_before: Optional[Dict[str, Any]] = None
            all_before: Optional[Dict[str, Any]] = None
            pub_after: Optional[Dict[str, Any]] = None
            all_after: Optional[Dict[str, Any]] = None
            self_type: Optional[str] = None

            if is_root and snapshot_self and len(args) >= 1:
                try:
                    inst = args[0]
                    self_type = _qualtype(inst)
                    pub_before = _snapshot_object(inst, include_private=False)
                    all_before = _snapshot_object(inst, include_private=True)
                except Exception:
                    pub_before = all_before = None
                    self_type = None

            try:
                result = fn(*args, **kwargs)

                if is_root:
                    try:
                        # AFTER snapshots for self (if relevant)
                        if pub_before is not None and len(args) >= 1:
                            try:
                                pub_after = _snapshot_object(args[0], include_private=False)
                                all_after = _snapshot_object(args[0], include_private=True)
                            except Exception:
                                pub_after = all_after = None

                        nonlocal initialized, written
                        with counter_lock:
                            if not initialized:
                                existing = list(
                                    storage_path.glob(f"{prefix}__*{st.extension}")
                                )
                                written = len(existing)
                                initialized = True

                            if written < limit:
                                # --- Bound arg policy (self/cls) ---
                                if drop_first and len(args) >= 1:
                                    if isinstance(st, PickleStorage):
                                        stored_args = args[1:]
                                    else:
                                        # JSON/REPR: keep a literal-friendly placeholder
                                        stored_args = (repr(args[0]),) + args[1:]
                                else:
                                    stored_args = args
                                # ----------------------------------

                                # ---- Optional capture of simple objects in inputs/outputs ----
                                obj_args_pos: Dict[int, dict] = {}
                                obj_args_kw: Dict[str, dict] = {}
                                if capture_objects != "off":
                                    # positionals (use call-site indices; rt.inject_object_args will adjust for self)
                                    for idx, val in enumerate(args):
                                        if drop_first and idx == 0:
                                            continue  # self/cls handled separately
                                        if not _is_builtin_like(val):
                                            spec = _obj_spec(val, include_private_objects, objects_stringify_depth)
                                            if spec:
                                                obj_args_pos[idx] = spec
                                    # keywords
                                    for k, v in (kwargs or {}).items():
                                        if not _is_builtin_like(v):
                                            spec = _obj_spec(v, include_private_objects, objects_stringify_depth)
                                            if spec:
                                                obj_args_kw[str(k)] = spec

                                result_obj = None
                                if capture_objects != "off" and not _is_builtin_like(result):
                                    tmp = _obj_spec(result, include_private_objects, objects_stringify_depth)
                                    if tmp:
                                        result_obj = tmp
                                # ----------------------------------------------------------------

                                entry: Dict[str, Any] = {
                                    "trace_schema": "pytead/v1",
                                    "func": fullname,
                                    "args": stored_args,
                                    "kwargs": kwargs,
                                    "result": result,
                                    "timestamp": datetime.utcnow().isoformat(
                                        timespec="microseconds"
                                    )
                                    + "Z",
                                }
                                if pub_before is not None:
                                    entry["self"] = {
                                        "type": self_type,
                                        # human-friendly (public-only) view
                                        "before": pub_before,
                                        "after": pub_after,
                                        # full state for rehydration (privates included)
                                        "state_before": all_before,
                                        "state_after": all_after,
                                    }
                                if capture_objects != "off":
                                    if obj_args_pos or obj_args_kw:
                                        entry["obj_args"] = {"pos": obj_args_pos, "kw": obj_args_kw}
                                    if result_obj is not None:
                                        entry["result_obj"] = result_obj

                                path = st.make_path(storage_path, fullname)
                                st.dump(entry, path)
                                written += 1
                    except Exception as exc:
                        _logger.error("Tracing failure for %s: %s", fullname, exc)

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

