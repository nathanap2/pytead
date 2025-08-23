from __future__ import annotations
import inspect
from dataclasses import dataclass
from typing import Any, Iterable, List, Set, Tuple

from .tracing import trace
from .errors import TargetResolutionError

import importlib


import logging
log = logging.getLogger("pytead.targets")

@dataclass(frozen=True)
class ResolvedTarget:
    owner: Any   # module or class that owns the attribute
    attr: str    # attribute name on owner
    kind: str    # "func" | "instancemethod" | "classmethod" | "staticmethod"





def _import_longest_prefix(fq: str):
    parts = fq.split(".")
    for i in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:i])
        try:
            mod = importlib.import_module(mod_name)
            return mod, parts[i:]
        except Exception:
            continue
    raise ImportError(f"Cannot import any prefix of '{fq}'")


def resolve_target(fq: str) -> ResolvedTarget:
    mod, tail = _import_longest_prefix(fq)
    if not tail:
        raise AttributeError(f"No attribute after module in '{fq}'")
    owner = mod
    for name in tail[:-1]:
        owner = getattr(owner, name)
    attr = tail[-1]

    raw = inspect.getattr_static(owner, attr)
    if isinstance(raw, staticmethod):
        kind = "staticmethod"
    elif isinstance(raw, classmethod):
        kind = "classmethod"
    elif inspect.isfunction(raw):
        kind = "instancemethod" if inspect.isclass(owner) else "func"
    else:
        kind = "func"  # conservative fallback
    return ResolvedTarget(owner=owner, attr=attr, kind=kind)


def instrument_targets(
    targets: Iterable[str], *, limit: int, storage_dir, storage
) -> Set[str]:
    """
    Resolve & instrument each target with the @trace decorator.

    - Supports plain functions, staticmethods and classmethods.
    - For descriptors (staticmethod/classmethod), unwrap the underlying function,
      decorate it, then reinstall the proper descriptor type.
    - Inserts detailed INFO logs to help diagnose which object/file was wrapped.
    - After installation, asserts (best-effort) that the installed object exposes
      a wrapped function (__wrapped__ present on the function or on __func__).

    Returns:
        Set of fully-qualified names that were instrumented.

    Raises:
        TargetResolutionError: if one or more targets could not be resolved,
        or if wrapping failed on some of them.
    """
    resolved: List[Tuple[ResolvedTarget, str]] = []
    errors: list[str] = []

    # 1) Resolve all targets first so we can surface a consolidated error
    for t in targets:
        try:
            rt = resolve_target(t)
            resolved.append((rt, t))
            owner_file = getattr(rt.owner, "__file__", None)
            owner_name = getattr(rt.owner, "__name__", type(rt.owner).__name__)
            log.info("Resolved %s -> owner=%s file=%s kind=%s",
                     t, owner_name, owner_file, rt.kind)
        except Exception as exc:
            errors.append(f"Cannot resolve target '{t}': {exc}")

    if errors:
        raise TargetResolutionError("\n".join(errors))

    # 2) Decorate & reinstall
    seen: Set[str] = set()
    for rt, fq in resolved:
        name = rt.attr
        try:
            # classify with getattr_static (doesn't trigger descriptors)
            raw_static = None
            try:
                raw_static = inspect.getattr_static(rt.owner, name)
            except Exception:
                pass

            if isinstance(raw_static, staticmethod):
                inner = raw_static.__func__
                wrapped = trace(limit=limit, storage_dir=storage_dir, storage=storage)(inner)
                setattr(rt.owner, name, staticmethod(wrapped))
            elif isinstance(raw_static, classmethod):
                inner = raw_static.__func__
                wrapped = trace(limit=limit, storage_dir=storage_dir, storage=storage)(inner)
                setattr(rt.owner, name, classmethod(wrapped))
            else:
                # plain function or any callable attribute
                fn = getattr(rt.owner, name)
                wrapped = trace(limit=limit, storage_dir=storage_dir, storage=storage)(fn)
                setattr(rt.owner, name, wrapped)

            # Post-condition: did we really install a wrapped function?
            try:
                installed = getattr(rt.owner, name)
                if isinstance(installed, (staticmethod, classmethod)):
                    base = installed.__func__
                else:
                    base = installed
                has_wrapped = hasattr(base, "__wrapped__")
            except Exception:
                has_wrapped = False

            owner_name = getattr(rt.owner, "__name__", type(rt.owner).__name__)
            log.info("Wrapped %s on %s (has_wrapped=%s)", name, owner_name, has_wrapped)

            seen.add(fq)
        except Exception as exc:
            errors.append(f"Failed to instrument '{fq}': {exc}")

    if errors:
        # If some failed, surface all messages together
        raise TargetResolutionError("\n".join(errors))

    return seen


