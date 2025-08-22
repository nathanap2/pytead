# pytead/tracing.py
from __future__ import annotations

import functools
import inspect
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union, cast

from .storage import PickleStorage, _to_literal as _to_literal

# Package-level logger stays quiet unless the host/CLI configures it.
_pkg_logger = logging.getLogger("pytead")
if not any(isinstance(h, logging.NullHandler) for h in _pkg_logger.handlers):
    _pkg_logger.addHandler(logging.NullHandler())
_logger = logging.getLogger("pytead.tracing")


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
):
    """
    Decorator that logs a callable's *root* calls so tests can be generated later.

    Behavior
    --------
    - Per-thread depth tracking ensures only the outermost call is recorded.
    - Pluggable Storage backend (pickle by default).
    - Uses a lightweight, versioned schema header ("pytead/v1").
    - **Instance methods**: if the first parameter is named 'self', automatically
      capture a shallow snapshot of the instance **BEFORE** and **AFTER** the call
      (attributes from __dict__ and __slots__, non-callables, private excluded).
    - **Full state for rehydration**: we also capture `state_before/state_after`
      including *private* attributes (converted via `_to_literal`) to be able to
      restore an instance later if needed.
    - **Pickle safety**: when using PickleStorage, we drop the bound first argument
      (`self` or `cls`) from the stored `args` to avoid pickling local classes/instances.

    Parameters
    ----------
    limit : int
        Max number of trace files written per decorated callable.
    storage_dir : str | Path
        Directory where trace files are written.
    storage : StorageLike | None
        Storage backend (defaults to PickleStorage()).
    """
    storage_path = Path(storage_dir)
    st = storage or PickleStorage()

    def _build_wrapper(fn: Callable[..., Any]) -> Callable[..., Any]:
        qual = getattr(fn, "__qualname__", getattr(fn, "__name__", "<lambda>"))
        fullname = f"{fn.__module__}.{qual}"
        prefix = fullname.replace(".", "_")

        # Inspect parameters once
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            has_pos0 = (
                len(params) >= 1
                and params[0].kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
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
                        if pub_before is not None and len(args) >= 1:
                            try:
                                pub_after = _snapshot_object(args[0], include_private=False)
                                all_after = _snapshot_object(args[0], include_private=True)
                            except Exception:
                                pub_after = all_after = None

                        nonlocal initialized, written
                        with counter_lock:
                            if not initialized:
                                existing = list(storage_path.glob(f"{prefix}__*{st.extension}"))
                                written = len(existing)
                                initialized = True

                            if written < limit:
                                # Keep bound arg only for JSON/REPR; drop for Pickle.
                                is_pickle = isinstance(st, PickleStorage)
                                stored_args = args[1:] if (is_pickle and drop_first and len(args) >= 1) else args

                                entry: Dict[str, Any] = {
                                    "trace_schema": "pytead/v1",
                                    "func": fullname,
                                    "args": stored_args,
                                    "kwargs": kwargs,
                                    "result": result,
                                    "timestamp": datetime.utcnow().isoformat(timespec="microseconds") + "Z",
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

