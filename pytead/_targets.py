# pytead/_targets.py
from __future__ import annotations
import importlib, inspect
from dataclasses import dataclass
from typing import Any, Tuple


@dataclass(frozen=True)
class ResolvedTarget:
    owner: Any  # module ou classe qui porte l'attribut
    attr: str  # nom d'attribut sur owner
    kind: str  # "func" | "instancemethod" | "classmethod" | "staticmethod"


def _import_longest_prefix(fq: str) -> Tuple[Any, list[str]]:
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
    """
    Accepte 'module.func' ou 'module.Class.method' (classes imbriquées ok).
    """
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
        kind = "func"  # fallback : objets/C-accelerated
    return ResolvedTarget(owner=owner, attr=attr, kind=kind)
