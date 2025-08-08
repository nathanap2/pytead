import functools
import logging
import pickle
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union, cast
import threading

# Configure logger for this module
_logger = logging.getLogger(__name__)
_handler = logging.StreamHandler()
_formatter = logging.Formatter("[pytead] %(levelname)s: %(message)s")
_handler.setFormatter(_formatter)
_logger.addHandler(_handler)
_logger.setLevel(logging.INFO)

Serializer = Callable[[Dict[str, Any], Path], None]


def _default_serializer(entry: Dict[str, Any], filepath: Path) -> None:
    """
    Serialize the entry as a pickle file at the specified filepath.
    """
    try:
        with filepath.open("wb") as f:
            pickle.dump(entry, f)
        _logger.debug("Log written to %s", filepath)
    except Exception as exc:
        _logger.error("Failed to write log %s: %s", filepath, exc)


def pytead(
    limit: int = 10,
    storage_dir: Union[str, Path] = Path("call_logs"),
    serializer: Optional[Serializer] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator that logs function calls to facilitate automatic test generation.

    :param limit: Maximum number of logged calls per function.
    :param storage_dir: Directory where logs will be stored.
    :param serializer: Optional custom serialization function (default: pickle).
    :return: A decorator to apply to the target function.

    Usage::

        @pytead(limit=5, storage_dir="logs")
        def my_function(x, y):
            return x + y
    """
    storage_path = Path(storage_dir)
    serializer = serializer or _default_serializer

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        fullname = f"{func.__module__}.{func.__name__}"
        prefix = fullname.replace(".", "_")
        
        local = threading.local()
        local.depth = 0

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            storage_path.mkdir(parents=True, exist_ok=True)

            local.depth += 1
            is_root = (local.depth == 1)
            try:
                result = func(*args, **kwargs)

                if is_root:
                    existing_logs = list(storage_path.glob(f"{prefix}__*.pkl"))
                    if len(existing_logs) < limit:
                        entry = {
                            "func": fullname,
                            "args": args,
                            "kwargs": kwargs,
                            "result": result,
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        filename = f"{prefix}__{uuid.uuid4().hex}.pkl"
                        serializer(entry, storage_path / filename)
            finally:
                local.depth -= 1
            return result

        return cast(Callable[..., Any], wrapper)

    return decorator
