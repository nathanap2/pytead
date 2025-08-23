# pytead/rt.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import importlib
import os
import sys

def _find_root(start: Path) -> Path:
    """
    Walk upward from 'start' to find a plausible project root:
    a directory that contains '.pytead' or 'pyproject.toml'.
    Fallback: parent of start.
    """
    cur = start.resolve()
    for p in [cur] + list(cur.parents):
        if (p / ".pytead").exists() or (p / "pyproject.toml").exists():
            return p
    return start.parent

def ensure_import_roots(here_file: str | os.PathLike[str], import_roots: Iterable[str | os.PathLike[str]]) -> None:
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

