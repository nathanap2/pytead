from __future__ import annotations
from typing import Any, Optional, NamedTuple, Iterable, List, Dict, Tuple
import textwrap
import pprint

_WRAP_WIDTH = 88



class Case7(NamedTuple):
    args: tuple
    kwargs: dict
    expected: Any
    self_type: Optional[str]
    self_state: Optional[dict]
    obj_args: Optional[dict]
    result_spec: Optional[dict]

def normalize_with_objs(e: Dict[str, Any]) -> Case7:
    """Extract a 7-tuple, tolerant to missing keys."""
    args = tuple(e.get("args", ()))
    kwargs = dict(e.get("kwargs") or {})
    expected = e.get("result")
    s = e.get("self") or {}
    return Case7(
        args=args,
        kwargs=kwargs,
        expected=expected,
        self_type=s.get("type"),
        self_state=s.get("state_before"),
        obj_args=e.get("obj_args") if isinstance(e.get("obj_args"), dict) else None,
        result_spec=e.get("result_obj") if isinstance(e.get("result_obj"), dict) else None,
    )

def unique_cases_with_objs(entries: Iterable[Dict[str, Any]]) -> List[Case7]:
    """Deduplicate by a stable repr-based key over the 7-tuple."""
    seen, out = set(), []
    for e in entries:
        c = normalize_with_objs(e)
        try:
            kw_items = tuple(sorted(c.kwargs.items()))
        except Exception:
            kw_items = tuple(c.kwargs.items())
        key = repr((c.args, kw_items, c.expected, c.self_type, c.self_state, c.obj_args, c.result_spec))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

def render_case_septuple(c: Case7, base_indent: int = 8) -> List[str]:
    """Return lines for one tuple literal, properly indented."""
    def pf(x):  # pretty-format with consistent width
        try:
            return pprint.pformat(x, width=88, compact=False, sort_dicts=True)
        except TypeError:
            return pprint.pformat(x, width=88, compact=False)
    indent_item = " " * base_indent
    indent_body = " " * (base_indent + 4)
    body = (
        f"{pf(c.args)},\n"
        f"{pf(c.kwargs)},\n"
        f"{pf(c.expected)},\n"
        f"{pf(c.self_type)},\n"
        f"{pf(c.self_state)},\n"
        f"{pf(c.obj_args)},\n"
        f"{pf(c.result_spec)},"
    )
    return [f"{indent_item}(", textwrap.indent(body, indent_body), f"{indent_item}),"]



def pformat(obj: Any, width: int = _WRAP_WIDTH, sort_dicts: bool = True) -> str:
    try:
        return pprint.pformat(obj, width=width, compact=False, sort_dicts=sort_dicts)
    except TypeError:
        return pprint.pformat(obj, width=width, compact=False)


def case_key_from_parts(args: tuple, kwargs: dict, expected: Any) -> str:
    try:
        kw_items = tuple(sorted(kwargs.items()))
    except Exception:
        kw_items = tuple(kwargs.items())
    return repr((args, kw_items, expected))


def normalize_entry(e: Dict[str, Any]) -> Tuple[tuple, dict, Any]:
    args = tuple(e.get("args", ()))
    kwargs = dict(e.get("kwargs", {}) or {})
    expected = e.get("result")
    return args, kwargs, expected


def unique_cases(entries: Iterable[Dict[str, Any]]) -> List[Tuple[tuple, dict, Any]]:
    seen, out = set(), []
    for e in entries:
        args, kwargs, expected = normalize_entry(e)
        k = case_key_from_parts(args, kwargs, expected)
        if k in seen:
            continue
        seen.add(k)
        out.append((args, kwargs, expected))
    return out


def case_id(args: tuple, kwargs: dict, maxlen: int = 80) -> str:
    base = repr(args) if not kwargs else f"{repr(args)} {repr(kwargs)}"
    return base if len(base) <= maxlen else base[: maxlen - 3] + "..."


def render_case_tuple(
    args: tuple, kwargs: dict, expected: Any, base_indent: int = 8
) -> list[str]:
    indent_item = " " * base_indent
    indent_body = " " * (base_indent + 4)
    body = f"{pformat(args)},\n{pformat(kwargs)},\n{pformat(expected)},"
    return [f"{indent_item}(", textwrap.indent(body, indent_body), f"{indent_item}),"]
