# pytead/rt.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import importlib
import os
import sys
from .imports import detect_project_root


def _find_root(start: Path) -> Path:
    return detect_project_root(start, fallback="parent")

def ensure_import_roots(
    here_file: str | os.PathLike[str], import_roots: Iterable[str | os.PathLike[str]]
) -> None:
    """
    Insert runtime import paths (script dir, project root, plus user-provided
    relative or absolute paths) at the front of sys.path, without duplicates.
    - Relative paths are resolved against the detected project root.
    """
    here = Path(here_file).resolve()
    root = _find_root(here)
    to_add: list[str] = []
    for raw in import_roots:
        s = str(raw)
        p = Path(s)
        abs_p = p if p.is_absolute() else (root / p)
        try:
            ap = str(abs_p.resolve())
        except Exception:
            ap = str(abs_p)
        if os.path.isdir(ap) and ap not in sys.path:
            to_add.append(ap)
    # maintain declared order
    for ap in to_add[::-1]:
        sys.path.insert(0, ap)


def resolve_attr(fq: str) -> Any:
    """
    Resolve a fully-qualified attribute: 'pkg.mod.Class.method' or 'pkg.mod.func'.
    Imports the longest module prefix and getattr through the remainder.
    """
    parts = fq.split(".")
    for i in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:i])
        try:
            obj = importlib.import_module(mod_name)
            rest = parts[i:]
            break
        except Exception:
            continue
    else:
        raise ImportError(f"Cannot import any prefix of {fq!r}")
    for name in rest:
        obj = getattr(obj, name)
    return obj


def rehydrate(type_fq: str, state: Optional[Dict[str, Any]]) -> Any:
    """
    Create an instance of 'type_fq' without calling __init__, then set attributes
    from the provided 'state' dict (best-effort, private names allowed).
    """
    mod_name, cls_name = type_fq.rsplit(".", 1)
    cls = getattr(importlib.import_module(mod_name), cls_name)
    inst = object.__new__(cls)
    for k, v in (state or {}).items():
        try:
            object.__setattr__(inst, k, v)
        except Exception:
            try:
                setattr(inst, k, v)
            except Exception:
                pass
    return inst


def drop_self_placeholder(args: tuple, self_type: Optional[str]) -> tuple:
    """
    Some non-pickle formats keep a human repr of 'self' as args[0]:
    e.g. '<MyClass object at 0x...>'. Drop it if it matches the class name.
    """
    if not args or not self_type or not isinstance(args[0], str):
        return args
    cls_name = self_type.rsplit(".", 1)[1]
    s0 = args[0]
    if s0.startswith("<") and cls_name in s0:
        return args[1:]
    return args


def inject_object_args(
    args: tuple, kwargs: dict, obj_args: dict | None, self_type: str | None
) -> tuple[tuple, dict]:
    """
    Replace positional/keyword arguments with rehydrated instances from obj_args:
      obj_args = {"pos": {idx: {"type": "...", "state": {...}}, ...},
                  "kw":  {name: {"type": "...", "state": {...}}, ...}}
    If a self placeholder is present at args[0] (JSON/REPR), indices in "pos" are
    shifted accordingly (the placeholder will be dropped before call).
    """
    if not obj_args:
        return args, kwargs

    pos = dict(obj_args.get("pos") or {})
    kw = dict(obj_args.get("kw") or {})

    # Detect whether args[0] is a self placeholder like "<Cls object at 0x...>"
    shift = 0
    if self_type and args and isinstance(args[0], str):
        cls_name = self_type.rsplit(".", 1)[-1]
        if args[0].startswith("<") and cls_name in args[0]:
            shift = 1

    lst = list(args)
    for idx, spec in pos.items():
        try:
            i = int(idx)
        except Exception:
            continue
        tgt = i - shift if shift and i > 0 else i
        if 0 <= tgt < len(lst):
            lst[tgt] = rehydrate(spec["type"], spec.get("state"))
    for k, spec in kw.items():
        if k in kwargs:
            kwargs[k] = rehydrate(spec["type"], spec.get("state"))
    return tuple(lst), kwargs


def assert_object_state(obj: Any, expected_state: Dict[str, Any]) -> None:
    """
    Assert that each attribute in expected_state exists on obj and equals the expected value.
    Produces readable assertion messages; ignores extra attributes on obj.
    """
    for k, v in (expected_state or {}).items():
        actual = getattr(obj, k)
        assert actual == v, f"attribute {k!r}: got {actual!r}, expected {v!r}"
