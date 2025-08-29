# pytead/_cases.py
from __future__ import annotations
from typing import Any, Optional, Iterable, List, Dict
from dataclasses import dataclass, field
import textwrap
import pprint

_WRAP_WIDTH = 88

#

def _to_hashable(obj: Any) -> Any:
    """
    What it does:
        Recursively converts common mutable collections into their immutable,
        hashable counterparts. Dictionaries are converted to deterministically
        sorted tuples of (key, value) pairs to ensure consistent hashing.

    Its role in the library:
        This is a critical helper function for the `TraceCase` dataclass.
        It allows `TraceCase` instances to be reliably hashed, which is essential
        for deduplicating identical function calls in the `unique_cases` function.
        Without this, you couldn't put cases with dicts or lists into a set.
    """
    if isinstance(obj, dict):
        # Sort items by a stable key (type name, then repr) to avoid
        # cross-type comparison errors and ensure deterministic order.
        items = [(k, _to_hashable(v)) for k, v in obj.items()]
        items.sort(key=lambda kv: (type(kv[0]).__name__, repr(kv[0])))
        return tuple(items)

    if isinstance(obj, (list, tuple)):
        return tuple(_to_hashable(v) for v in obj)

    if isinstance(obj, set):
        return frozenset(_to_hashable(v) for v in obj)

    if isinstance(obj, (bytearray, memoryview)):
        return bytes(obj)

    return obj


@dataclass(frozen=True)
class TraceCase:
    """
    What it does:
        A frozen dataclass representing a single, unique traced call. It stores
        the inputs (args, kwargs), the output (expected), and optional state
        information for methods (`self_type`, `self_state`) and complex objects
        (`obj_args`, `result_spec`).

    Its role in the library:
        This is the primary data structure for legacy, state-based tests. The
        `from_entry` classmethod is used to convert raw trace dictionaries into
        these structured objects. Its custom `__hash__` and `__eq__` methods,
        powered by `_to_hashable`, are the foundation of test case deduplication.
    """
    args: tuple
    kwargs: dict
    expected: Any
    self_type: Optional[str] = None
    self_state: Optional[dict] = None
    obj_args: Optional[dict] = None
    result_spec: Optional[dict] = None
    
    # This field will store the hashable representation of the instance.
    _key: tuple = field(init=False, repr=False, hash=False, compare=False)

    def __post_init__(self):
        """Computes a stable hash key after initialization."""
        try:
            # Sort kwargs by key to ensure order doesn't affect hashing.
            kw_items = tuple(sorted(self.kwargs.items(), key=lambda item: str(item[0])))
        except TypeError:
            # Fallback for un-sortable keys (less common).
            kw_items = tuple(self.kwargs.items())

        # Use `object.__setattr__` because the dataclass is frozen.
        object.__setattr__(
            self,
            "_key",
            (
                _to_hashable(self.args),
                _to_hashable(kw_items),
                _to_hashable(self.expected),
                self.self_type,
                _to_hashable(self.self_state),
                _to_hashable(self.obj_args),
                _to_hashable(self.result_spec),
            ),
        )

    def __hash__(self):
        return hash(self._key)

    def __eq__(self, other):
        if not isinstance(other, TraceCase):
            return NotImplemented
        return self._key == other._key

    @classmethod
    def from_entry(cls, entry: Dict[str, Any]) -> "TraceCase":
        """Creates a TraceCase instance from a raw trace entry dictionary."""
        self_data = entry.get("self") or {}
        return cls(
            args=tuple(entry.get("args", ())),
            kwargs=dict(entry.get("kwargs") or {}),
            expected=entry.get("result"),
            self_type=self_data.get("type"),
            self_state=self_data.get("state_before"),
            obj_args=entry.get("obj_args") if isinstance(entry.get("obj_args"), dict) else None,
            result_spec=entry.get("result_obj") if isinstance(entry.get("result_obj"), dict) else None,
        )


def unique_cases(entries: Iterable[Dict[str, Any]]) -> List[TraceCase]:
    """
    What it does:
        Loads, normalizes, and deduplicates test cases from raw trace entries.

    Its role in the library:
        This is the main processor for legacy trace files. `gen_tests.py` calls
        this function to get a clean, unique list of `TraceCase` objects before
        rendering the test file. This ensures that if a function was called 100
        times with the same inputs and gave the same output, only one test is generated.
    """
    # Using a dict as an ordered set for efficient deduplication.
    cases = list({TraceCase.from_entry(e): None for e in entries}.keys())
    
    # A sanity check to catch any unhashable types that _to_hashable might have missed.
    for case in cases:
        try:
            hash(case)
        except TypeError as e:
            raise TypeError(
                f"Failed to hash a TraceCase. This likely means its '_key' "
                f"contains an unhashable type that _to_hashable missed.\n"
                f"Offending case args: {case.args!r}\n"
                f"Offending case expected: {case.expected!r}\n"
                f"Original error: {e}"
            ) from e
            
    return cases


def pformat(obj: Any, width: int = _WRAP_WIDTH, sort_dicts: bool = True) -> str:
    """
    What it does:
        A robust wrapper around `pprint.pformat` that falls back to non-sorted
        dicts if sorting fails (e.g., with mixed-type keys).

    Its role in the library:
        A formatting helper used by `render_case` to ensure that data structures
        are pretty-printed in the generated test code, improving readability.
    """
    try:
        return pprint.pformat(obj, width=width, compact=False, sort_dicts=sort_dicts)
    except TypeError:
        return pprint.pformat(obj, width=width, compact=False)


def render_case(case: TraceCase, base_indent: int = 8) -> List[str]:
    """
    What it does:
        Generates the Python code for a single `TraceCase` as a multi-line,
        indented tuple literal.

    Its role in the library:
        This function is called in a loop by `_render_legacy_tests` (in `gen_tests.py`)
        to build the list of test cases (e.g., `CASES_mymodule_myfunc = [...]`)
        that will be used by `pytest.mark.parametrize`.
    """
    indent_item = " " * base_indent
    indent_body = " " * (base_indent + 4)
    
    body = (
        f"{pformat(case.args)},\n"
        f"{pformat(case.kwargs)},\n"
        f"{pformat(case.expected)},\n"
        f"{pformat(case.self_type)},\n"
        f"{pformat(case.self_state)},\n"
        f"{pformat(case.obj_args)},\n"
        f"{pformat(case.result_spec)},"
    )
    
    return [f"{indent_item}(", textwrap.indent(body, indent_body), f"{indent_item}),"]


def case_id(args: tuple, kwargs: dict, maxlen: int = 80) -> str:
    """
    What it does:
        Generates a human-readable, truncated string representation of a call's
        arguments for use as a test ID.

    Its role in the library:
        This function provides the friendly names for parameterized tests that
        appear in pytest's output (e.g., `... PASSED tests/test_mymodule.py::test_add[2-3]`).
        It's passed to the `ids` argument of `@pytest.mark.parametrize`.
    """
    base = repr(args) if not kwargs else f"{repr(args)} {repr(kwargs)}"
    return base if len(base) <= maxlen else base[: maxlen - 3] + "..."
