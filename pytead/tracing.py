import functools
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union, cast

from .storage import PickleStorage

# Package-level logger stays quiet unless the host/CLI configures it.
_pkg_logger = logging.getLogger("pytead")
if not any(isinstance(h, logging.NullHandler) for h in _pkg_logger.handlers):
    _pkg_logger.addHandler(logging.NullHandler())
_logger = logging.getLogger("pytead.tracing")


def trace(
    limit: int = 10,
    storage_dir: Union[str, Path] = Path("call_logs"),
    storage=None,
):
    """
    Decorator that logs a function's *root* calls so tests can be generated later.

    - Per-thread depth tracking ensures only the outermost call is recorded.
    - Pluggable Storage backend (pickle by default).
    - Uses a lightweight, versioned schema header ("pytead/v1").
    - Library never configures handlers (NullHandler only).
    """
    storage_path = Path(storage_dir)
    st = storage or PickleStorage()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        qual = getattr(func, "__qualname__", func.__name__)
        fullname = f"{func.__module__}.{qual}"
        prefix = fullname.replace(".", "_")

        # Per-thread depth sentinel to detect root vs nested calls
        local = threading.local()

        # Per-decorated-function counter: avoid hitting the filesystem on every call.
        counter_lock = threading.Lock()
        initialized = False
        written = 0  # number of entries already persisted for this function

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # ---- Depth handling (thread-local) ----
            prev_depth = getattr(local, "depth", 0)
            setattr(local, "depth", prev_depth + 1)
            is_root = prev_depth == 0

            try:
                result = func(*args, **kwargs)

                if is_root:
                    try:
                        # Initialize once by scanning existing files of this function/format.
                        nonlocal initialized, written
                        with counter_lock:
                            if not initialized:
                                existing = list(
                                    storage_path.glob(f"{prefix}__*{st.extension}")
                                )
                                written = len(existing)
                                initialized = True

                            if written < limit:
                                entry: Dict[str, Any] = {
                                    "trace_schema": "pytead/v1",
                                    "func": fullname,
                                    "args": args,
                                    "kwargs": kwargs,
                                    "result": result,
                                    "timestamp": datetime.utcnow().isoformat(
                                        timespec="microseconds"
                                    )
                                    + "Z",
                                }
                                path = st.make_path(storage_path, fullname)
                                st.dump(entry, path)
                                written += 1
                    except Exception as exc:  # tracing must never break user code
                        _logger.error("Tracing failure for %s: %s", fullname, exc)

                return result

            finally:
                # ---- Safe decrement / cleanup ----
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

    return decorator
