# pytead/graph_capture.py

from __future__ import annotations
from typing import Any, Dict, Optional
import logging
import re
from .errors import GraphCaptureRefToUnanchored

from .graph_utils import project_anchored_to_rendered, find_orphan_refs_in_rendered

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
def capture_anchored_graph(
    obj: Any,
    *,
    max_depth: int = 5,
    _memo: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Build an **anchored graph** (internal IR):

    - Every referencable node receives a unique `$id` anchor (dicts/objects, lists,
      tuples, sets/frozensets, and mappings with non-JSON keys encoded via `{"$map": ...}`).
    - Repeated references are encoded as `{"$ref": id}`.
    - Depth guard: when `max_depth` is exhausted, emit a stable textual placeholder.

    Determinism
    -----------
    - Dicts with JSON-able keys keep insertion order.
    - `$map` pairs and `$set` elements are sorted by `repr` for stable output.

    Notes
    -----
    - Pure (does not mutate inputs); `_memo` is an internal alias/cycle tracker.
    """
    if _memo is None:
        _memo = {"labels": {}, "next": 1}

    # Scalars or depth limit
    if _is_scalar(obj):
        return obj
    if max_depth <= 0:
        return _safe_repr_or_classname(obj)

    oid = id(obj)
    labels: Dict[int, int] = _memo["labels"]

    # Seen before → back-reference
    if oid in labels:
        return {"$ref": labels[oid]}

    # First time → allocate anchor
    label = _memo["next"]
    labels[oid] = label
    _memo["next"] = label + 1

    # Dict: JSON keys vs non-JSON keys ($map)
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj.keys()):
            node: Dict[str, Any] = {"$id": label}
            for k, v in obj.items():  # insertion order preserved
                node[k] = capture_anchored_graph(v, max_depth=max_depth - 1, _memo=_memo)
            return node
        else:
            pairs: list[list[Any]] = []
            for k, v in obj.items():
                kg = capture_anchored_graph(k, max_depth=max_depth - 1, _memo=_memo)
                vg = capture_anchored_graph(v, max_depth=max_depth - 1, _memo=_memo)
                pairs.append([kg, vg])
            pairs.sort(key=lambda kv: repr(kv[0]))  # deterministic order
            return {"$id": label, "$map": pairs}

    # List
    if isinstance(obj, list):
        return {
            "$id": label,
            "$list": [capture_anchored_graph(x, max_depth=max_depth - 1, _memo=_memo) for x in obj],
        }

    # Tuple
    if isinstance(obj, tuple):
        return {
            "$id": label,
            "$tuple": [capture_anchored_graph(x, max_depth=max_depth - 1, _memo=_memo) for x in obj],
        }

    # Set / FrozenSet
    if isinstance(obj, (set, frozenset)):
        elems = [capture_anchored_graph(x, max_depth=max_depth - 1, _memo=_memo) for x in obj]
        elems.sort(key=repr)  # deterministic
        return {"$id": label, "$set": elems, "$frozen": isinstance(obj, frozenset)}

    # Custom objects: capture public, non-callable attributes
    node: Dict[str, Any] = {"$id": label}
    for key, value in _get_object_attributes(obj).items():
        if key.startswith("_") or callable(value):
            continue
        try:
            node[key] = capture_anchored_graph(value, max_depth=max_depth - 1, _memo=_memo)
        except Exception:
            node[key] = "<_capture_error_>"
    return node


# ------------------- API publique : projection v1 selon le contexte -------------------

def capture_object_graph(obj: Any, *, max_depth: int = 5) -> Any:
    """
    Produce a *rendered graph* for `obj`, suitable for embedding in tests and logs.

    Pipeline
    --------
    1) Capture an **anchored graph** by exploring `obj` up to `max_depth`
       (nodes carry `$id` anchors and references may appear as `{"$ref": N}`).
    2) Project it to a **rendered graph** in *capture* mode:
       - drop all `$id` anchors,
       - unwrap `$list` / `$tuple` / `$set` / `$map` markers into JSON-like shapes,
       - **preserve** `{"$ref": N}` nodes so aliasing can still be represented
         without anchors.

    Behavior
    --------
    This routine is non-throwing. If the projection yields references that have no
    surviving anchor in the rendered view, they are kept as-is and a WARNING may be
    logged on the `pytead.graph_capture` logger. Use `capture_object_graph_checked(...)`
    if you prefer to raise on such cases.

    Parameters
    ----------
    obj : Any
        The Python value to capture.
    max_depth : int, default 5
        Maximum traversal depth for the anchored capture. Scalars are always recorded;
        when the limit is reached, complex values are summarized with a stable textual
        placeholder.

    Returns
    -------
    Any
        A JSON-like rendered graph (dicts/lists/scalars and the special shapes
        `{"$map": [...]}` and `{"$set": [...], "$frozen": bool}`), possibly containing
        `{"$ref": N}` entries when aliasing was detected.

    Notes
    -----
    - The function does not mutate `obj`.
    - Output is deterministic (e.g., `$map` and `$set` elements are deterministically ordered).
    """
    core = capture_anchored_graph(obj, max_depth=max_depth)
    return project_anchored_to_rendered(core, mode="capture", tuples_as_lists=False, warn_logger=_log)


def capture_object_graph_checked(obj: Any, *, max_depth: int = 5) -> Any:
    """
    Capture an anchored graph, project it to a rendered graph (capture mode),
    and **raise** if the projection leaves any orphan `{"$ref": N}`.

    Returns
    -------
    Any
        The rendered graph.

    Raises
    ------
    GraphCaptureRefToUnanchored
        If at least one reference has no corresponding `$id` anchor after projection.
    """
    core = capture_anchored_graph(obj, max_depth=max_depth)
    rendered = project_anchored_to_rendered(
        core, mode="capture", tuples_as_lists=False, warn_logger=_log
    )
    orphans = find_orphan_refs_in_rendered(rendered)
    if orphans:
        details = "; ".join(f"path={p} ref={rid}" for p, rid in orphans)
        raise GraphCaptureRefToUnanchored(
            f"capture produced orphan $ref(s) after rendered projection: {details}"
        )
    return rendered

