from typing import Any, Dict, Iterable, List, Tuple
import pprint
import textwrap

_WRAP_WIDTH = 88


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
