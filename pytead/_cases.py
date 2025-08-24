# pytead/_cases.py
from __future__ import annotations
from typing import Any, Optional, Iterable, List, Dict
from dataclasses import dataclass, field
import textwrap
import pprint

_WRAP_WIDTH = 88

def _to_hashable(obj: Any) -> Any:
    """Recursively convert mutable or unhashable collections to hashable types."""
    if isinstance(obj, dict):
        # Convert dict to a sorted tuple of (key, hashable_value) pairs
        return tuple(sorted((k, _to_hashable(v)) for k, v in obj.items()))
    
    if isinstance(obj, (list, tuple)):
        # Convert list/tuple to a tuple of hashable_values
        return tuple(_to_hashable(v) for v in obj)
        
    if isinstance(obj, set):
        # Convert set to a frozenset of hashable_values
        return frozenset(_to_hashable(v) for v in obj)
    
    # It's a primitive or an object, assume it's hashable
    return obj

@dataclass(frozen=True)
class TraceCase:
    """Represents a single, unique traced test case."""
    args: tuple
    kwargs: dict
    expected: Any
    self_type: Optional[str] = None
    self_state: Optional[dict] = None
    obj_args: Optional[dict] = None
    result_spec: Optional[dict] = None
    
    _key: tuple = field(init=False, repr=False, hash=False, compare=False)

    def __post_init__(self):
        """Computes a stable hash key after initialization."""
        try:
            # Convert kwargs dict to a stable, hashable tuple of items
            kw_items = tuple(sorted(self.kwargs.items(), key=lambda item: str(item[0])))
        except TypeError:
            kw_items = tuple(self.kwargs.items())

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
    """Loads, normalizes, and deduplicates test cases from trace entries."""
    cases = list({TraceCase.from_entry(e): None for e in entries}.keys())
    
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
    """Wrapper around pprint.pformat to handle sorting errors."""
    try:
        return pprint.pformat(obj, width=width, compact=False, sort_dicts=sort_dicts)
    except TypeError:
        return pprint.pformat(obj, width=width, compact=False)


def render_case(case: TraceCase, base_indent: int = 8) -> List[str]:
    """Generates the code lines for a single TraceCase tuple literal."""
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
    """Generates a human-readable test ID for pytest."""
    base = repr(args) if not kwargs else f"{repr(args)} {repr(kwargs)}"
    return base if len(base) <= maxlen else base[: maxlen - 3] + "..."
