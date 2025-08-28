# pytead/graph_capture.py

from __future__ import annotations
from typing import Any, Dict, Optional
import logging
import re
from .errors import GraphCaptureRefToUnanchored

from .graph_utils import project_v2_to_v1, find_orphan_refs

_log = logging.getLogger("pytead.graph_capture")
_OPAQUE_REPR_RE = re.compile(r"^<[\w\.]+ object at 0x[0-9A-Fa-f]+>$")

def _safe_repr_or_classname(obj: Any) -> str:
    try:
        r = repr(obj)
    except Exception:
        r = ""
    if r and not _OPAQUE_REPR_RE.match(r):
        return r
    t = type(obj)
    name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
    return f"<{t.__module__}.{name}>"

def _get_object_attributes(obj: Any) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    if hasattr(obj, "__dict__"):
        attrs.update(vars(obj))
    if hasattr(obj, "__slots__"):
        for name in obj.__slots__:
            try:
                if name not in attrs:
                    attrs[name] = getattr(obj, name)
            except AttributeError:
                pass
    return attrs

def _is_scalar(x: Any) -> bool:
    return x is None or isinstance(x, (bool, int, float, str, bytes))

# ------------------------- CAPTURE IR v2 (tout ancré) -------------------------

def capture_object_graph_v2(
    obj: Any,
    *,
    max_depth: int = 5,
    _memo: Optional[Dict[str, Any]] = None
) -> Any:
    """
    Capture en IR v2 : tout nœud référencable reçoit un '$id' (dict/objets, listes,
    tuples, sets, mappings non-JSON via '$map'). Les réutilisations émettent {'$ref': id}.
    """
    if _memo is None:
        _memo = {"labels": {}, "next": 1}

    # Profondeur / feuilles

    if _is_scalar(obj):
        return obj
    if max_depth <= 0:
        return _safe_repr_or_classname(obj)

    oid = id(obj)
    labels: Dict[int, int] = _memo["labels"]

    # Déjà vu → ref
    if oid in labels:
        return {"$ref": labels[oid]}

    # Première rencontre → label
    label = _memo["next"]
    labels[oid] = label
    _memo["next"] = label + 1

    # Dict (clés str → objet JSON; sinon → $map)
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj.keys()):
            node: Dict[str, Any] = {"$id": label}
            for k, v in obj.items():  # ordre d’insertion stable
                node[k] = capture_object_graph_v2(v, max_depth=max_depth - 1, _memo=_memo)
            return node
        else:
            pairs: list[list[Any]] = []
            for k, v in obj.items():
                kg = capture_object_graph_v2(k, max_depth=max_depth - 1, _memo=_memo)
                vg = capture_object_graph_v2(v, max_depth=max_depth - 1, _memo=_memo)
                pairs.append([kg, vg])
            pairs.sort(key=lambda kv: repr(kv[0]))  # déterministe
            return {"$id": label, "$map": pairs}

    # List
    if isinstance(obj, list):
        return {
            "$id": label,
            "$list": [capture_object_graph_v2(x, max_depth=max_depth - 1, _memo=_memo) for x in obj],
        }

    # Tuple
    if isinstance(obj, tuple):
        return {
            "$id": label,
            "$tuple": [capture_object_graph_v2(x, max_depth=max_depth - 1, _memo=_memo) for x in obj],
        }

    # Set / FrozenSet
    if isinstance(obj, (set, frozenset)):
        elems = [capture_object_graph_v2(x, max_depth=max_depth - 1, _memo=_memo) for x in obj]
        elems.sort(key=repr)
        return {"$id": label, "$set": elems, "$frozen": isinstance(obj, frozenset)}

    # Objets “custom” : on capture les attributs publics non-callables
    node: Dict[str, Any] = {"$id": label}
    for key, value in _get_object_attributes(obj).items():
        if key.startswith("_") or callable(value):
            continue
        try:
            node[key] = capture_object_graph_v2(value, max_depth=max_depth - 1, _memo=_memo)
        except Exception:
            node[key] = "<_capture_error_>"
    return node


# ------------------- API publique : projection v1 selon le contexte -------------------

def capture_object_graph(obj: Any, *, max_depth: int = 5) -> Any:
    """
    API *publique* (compatible tests v1) :
      1) capture en IR v2,
      2) projection v1 (mode 'capture') : strip $id, unwrap, conserver les {$ref}.
         On loggue un WARNING pour toute ref devenue “orpheline” dans la vue v1.
    """
    core = capture_object_graph_v2(obj, max_depth=max_depth)
    return project_v2_to_v1(core, mode="capture", tuples_as_lists=False, warn_logger=_log)

def capture_object_graph_checked(obj: Any, *, max_depth: int = 5) -> Any:
    """
    Capture V2, puis **projette en V1 (mode 'capture')** et lève si des $ref
    deviennent orphelines dans cette projection (cas typique : aliasing de liste/tuple).
    """
    core_v2 = capture_object_graph_v2(obj, max_depth=max_depth)
    v1 = project_v2_to_v1(core_v2, mode="capture", tuples_as_lists=False, warn_logger=_log)
    orphans = find_orphan_refs(v1)
    if orphans:
        details = "; ".join(f"path={p} ref={rid}" for p, rid in orphans)
        raise GraphCaptureRefToUnanchored(f"capture produced orphan $ref(s) after v1 projection: {details}")
    return v1
