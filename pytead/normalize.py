from __future__ import annotations
from typing import Any
import math

def sanitize_for_py_literals(obj: Any) -> Any:
    """Return a copy of *obj* where floating NaN and ±Inf are replaced by ``None``.

    The transformation recurses into lists, tuples, and dicts. All other types are
    returned unchanged. This makes values safe to embed as Python literals in
    generated test files.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, list):
        return [sanitize_for_py_literals(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_for_py_literals(x) for x in obj)
    if isinstance(obj, dict):
        return {k: sanitize_for_py_literals(v) for k, v in obj.items()}
    return obj

def tuples_to_lists(obj: Any) -> Any:
    """Return a copy of *obj* with all tuples converted to lists recursively.

    Useful for producing JSON-like representations of nested structures.
    """
    if isinstance(obj, tuple):
        return [tuples_to_lists(x) for x in obj]
    if isinstance(obj, list):
        return [tuples_to_lists(x) for x in obj]
    if isinstance(obj, dict):
        return {k: tuples_to_lists(v) for k, v in obj.items()}
    return obj
