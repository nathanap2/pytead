# pytead/tracing.py
from __future__ import annotations

import functools
import inspect
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union, cast
import re

from .graph_capture import capture_object_graph, capture_object_graph_v2
from .storage import PickleStorage, GraphJsonStorage, _to_literal as _to_literal


class OrphanRefInTrace(RuntimeError):
    """A {'$ref': N} appears in the trace but no matching {'$id': N} exists
    in result/args/kwargs after best-effort fix. The trace must not be written.
    """

# Package-level logger stays quiet unless the host/CLI configures it.
_pkg_logger = logging.getLogger("pytead")
if not any(isinstance(h, logging.NullHandler) for h in _pkg_logger.handlers):
    _pkg_logger.addHandler(logging.NullHandler())
_logger = logging.getLogger("pytead.tracing")










# --- helpers pour inline depuis les donneurs (args/kwargs) ---
def _build_ref_donor_index(donors_graphs):
    """Collecte {id -> ancre_dict} sur tous les donneurs."""
    index = {}

    def walk(node):
        if isinstance(node, dict):
            if "$id" in node and isinstance(node["$id"], int):
                index[node["$id"]] = node
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    for g in donors_graphs:
        walk(g)
    return index


def _inline_refs_from_donors(node, donor_index):
    """Retourne une *copie* de node où chaque {'$ref': N} présent dans donor_index
    est remplacé par une *copie profonde* de l'ancre correspondante.
    """
    if isinstance(node, dict):
        # Cas feuille: uniquement {'$ref': N}
        if set(node.keys()) == {"$ref"} and isinstance(node["$ref"], int):
            rid = node["$ref"]
            if rid in donor_index:
                return copy.deepcopy(donor_index[rid])
            return node  # pas de donneur connu -> on laisse tel quel
        # Sinon on descend récursivement
        return {k: _inline_refs_from_donors(v, donor_index) for k, v in node.items()}
    elif isinstance(node, list):
        return [_inline_refs_from_donors(x, donor_index) for x in node]
    elif isinstance(node, tuple):
        return tuple(_inline_refs_from_donors(x, donor_index) for x in node)
    else:
        return node


def _validate_or_fix_graphjson_entry(entry, strict_mode="error"):
    """Best-effort pour supprimer toute ref orpheline dans result_graph,
    puis validation stricte. Renvoie *une copie* potentiellement corrigée.
    strict_mode: "error" (défaut) | "warn"  (si tu veux dégrader en warning).
    """
    func = entry.get("func", "<unknown>")
    args_g = entry.get("args_graph", [])
    kwargs_g = entry.get("kwargs_graph", {})
    result_g = entry.get("result_graph", None)

    # 1) Tentative de correction : inliner les $ref qui pointent vers des ancres des donneurs
    donor_index = _build_ref_donor_index([args_g, kwargs_g])
    fixed_result = _inline_refs_from_donors(result_g, donor_index)

    # 2) Validation : plus aucune ref orpheline en considérant args/kwargs comme donneurs
    orphans = find_orphan_refs(fixed_result, donors_graphs=[args_g, kwargs_g])
    if orphans:
        msg = (
            f"ORPHAN_REF in trace for {func}: {len(orphans)} orphan(s): "
            + ", ".join(f"{p} -> ref={rid}" for p, rid in orphans)
        )
        if strict_mode == "warn":
            log.warning(msg)
        else:
            # mode "error" par défaut
            raise OrphanRefInTrace(msg)

    # 3) Retourne une copie de l'entry avec result_graph corrigé
    fixed = dict(entry)
    fixed["result_graph"] = fixed_result
    return fixed
    
def _build_graphjson_entry_unified(func_qualname, args, kwargs, result):
    """
    Capture v2 *en une passe* de (args, kwargs, result) pour que les IDs
    soient *partagés* entre toutes les sections. Ça évite les $ref vers des
    IDs qui n'existent que dans une autre passe de capture.
    """
    # On emballe pour capturer en une seule fois
    bundle = {
        "__args__": list(args),
        "__kwargs__": dict(kwargs),
        "__result__": result,
    }
    g = capture_object_graph(bundle, max_depth=5)  # même depth que le reste du projet

    # On "dépaquette"
    args_graph = g.get("__args__", [])
    kwargs_graph = g.get("__kwargs__", {})
    result_graph = g.get("__result__", None)

    # Métadonnées + timestamp
    entry = {
        "trace_schema": "pytead/v2-graph",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "func": func_qualname,
        "args_graph": args_graph,
        "kwargs_graph": kwargs_graph,
        "result_graph": result_graph,
    }

    # Garde-fou : par défaut, on exige l'absence de $ref orphelin
    strict = os.getenv("PYTEAD_STRICT_GRAPH_JSON_REFS", "error").lower()
    return _validate_or_fix_graphjson_entry(entry, strict_mode=strict)




# ---------------------- Formatting helpers (unchanged behavior) ----------------------

_OPAQUE_REPR_RE = re.compile(r"^<[\w\.]+ object at 0x[0-9A-Fa-f]+>$")



def _is_simple_literal(value: Any) -> bool:
    return value is None or isinstance(
        value, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice)
    )

def _safe_repr_or_classname(x: Any) -> str:
    """
    Prefer a meaningful repr; if it looks like the default '<Pkg.Class object at 0x...>',
    fall back to the fully-qualified class name.
    """
    try:
        r = repr(x)
    except Exception:
        r = None
    if not r:
        t = type(x)
        name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
        return f"{t.__module__}.{name}" if t.__module__ and t.__module__ != "builtins" else name
    if _OPAQUE_REPR_RE.match(r):
        t = type(x)
        name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
        return f"{t.__module__}.{name}" if t.__module__ and t.__module__ != "builtins" else name
    return r


def _stringify_level1(value: Any) -> Any:
    """
    Depth=1 stringify: turn non-builtin objects into strings (repr-or-classname).
    For builtin containers, apply the same conversion to their direct elements,
    without recursing deeper.
    """
    # Scalars / bytes-likes / None → keep literal-friendly form via _to_literal
    if value is None or isinstance(
        value, (bool, int, float, complex, str, bytes, bytearray, memoryview, range, slice)
    ):
        return _to_literal(value)

    # Builtin containers: map only one level
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kk = _to_literal(k)  # keys should stay literal
            vv = (
                _safe_repr_or_classname(v)
                if not isinstance(
                    v,
                    (
                        bool,
                        int,
                        float,
                        complex,
                        str,
                        bytes,
                        bytearray,
                        memoryview,
                        range,
                        slice,
                        list,
                        tuple,
                        set,
                        frozenset,
                        dict,
                    ),
                )
                else _to_literal(v)
            )
            # one-level container mapping for direct elements
            if isinstance(v, (list, tuple, set, frozenset)):
                vv = [
                    _safe_repr_or_classname(e)
                    if not isinstance(
                        e,
                        (
                            bool,
                            int,
                            float,
                            complex,
                            str,
                            bytes,
                            bytearray,
                            memoryview,
                            range,
                            slice,
                            list,
                            tuple,
                            set,
                            frozenset,
                            dict,
                        ),
                    )
                    else _to_literal(e)
                    for e in v
                ]
                if isinstance(v, tuple):
                    vv = tuple(vv)
                if isinstance(v, (set, frozenset)):
                    vv = sorted(vv)  # determinism
            elif isinstance(v, dict):
                vv = {
                    _to_literal(kk2): (
                        _safe_repr_or_classname(vv2)
                        if not isinstance(
                            vv2,
                            (
                                bool,
                                int,
                                float,
                                complex,
                                str,
                                bytes,
                                bytearray,
                                memoryview,
                                range,
                                slice,
                                list,
                                tuple,
                                set,
                                frozenset,
                                dict,
                            ),
                        )
                        else _to_literal(vv2)
                    )
                    for kk2, vv2 in v.items()
                }
            out[kk] = vv
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        seq = [
            _safe_repr_or_classname(e)
            if not isinstance(
                e,
                (
                    bool,
                    int,
                    float,
                    complex,
                    str,
                    bytes,
                    bytearray,
                    memoryview,
                    range,
                    slice,
                    list,
                    tuple,
                    set,
                    frozenset,
                    dict,
                ),
            )
            else _to_literal(e)
            for e in value
        ]
        if isinstance(value, (set, frozenset)):
            seq = sorted(seq)  # determinism
        return tuple(seq) if isinstance(value, tuple) else list(seq)

    # Any other (probably user-defined) object → string
    return _safe_repr_or_classname(value)


def _qualtype(obj: Any) -> str:
    """Return a readable fully-qualified type name for an instance."""
    t = type(obj)
    name = getattr(t, "__qualname__", getattr(t, "__name__", str(t)))
    return f"{t.__module__}.{name}" if t.__module__ and t.__module__ != "builtins" else name


def _iter_slots(cls: type) -> list[str]:
    """Collect __slots__ names along the MRO (handles str or iterable forms)."""
    names: list[str] = []
    try:
        for c in cls.mro():
            s = getattr(c, "__slots__", ())
            if not s:
                continue
            if isinstance(s, str):
                names.append(s)
            else:
                names.extend(list(s))
    except Exception:
        # Be conservative; snapshotting must never break user code.
        pass
    return names


def _snapshot_object(obj: Any, include_private: bool = False) -> Dict[str, Any]:
    """
    Shallow snapshot of an object's state as {attr_name: literal_value}.

    - Uses __dict__ when available.
    - Completes with __slots__ when present.
    - Skips callables and descriptors.
    - Best-effort: any failure on an attribute is ignored.
    - Values are converted to literal-ish forms via _to_literal.
    """
    snap: Dict[str, Any] = {}

    # __dict__
    try:
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            for k, v in d.items():
                if not include_private and str(k).startswith("_"):
                    continue
                try:
                    if callable(v):
                        continue
                    snap[k] = _to_literal(v)
                except Exception:
                    # Never raise from snapshotting.
                    pass
    except Exception:
        pass

    # __slots__
    try:
        for name in _iter_slots(type(obj)):
            if not include_private and str(name).startswith("_"):
                continue
            if name in snap:
                continue
            try:
                v = getattr(obj, name)
            except Exception:
                continue
            try:
                if callable(v):
                    continue
                snap[name] = _to_literal(v)
            except Exception:
                pass
    except Exception:
        pass

    return snap


# ---------------------- New: extracted trace core helpers ----------------------

@dataclass(frozen=True)
class _TracePolicy:
    limit: int
    storage_dir: Path
    storage: Any
    capture_objects: str                 # "off" | "simple"
    include_private_objects: bool
    objects_stringify_depth: int


def _is_builtin_like(x: Any) -> bool:
    """
    True if `x` is a literal-safe scalar or a builtin container (we don't snapshot it).
    """
    if x is None or isinstance(x, (str, bytes, bytearray, memoryview, bool, int, float, complex)):
        return True
    if isinstance(x, (range, slice)):
        return True
    if isinstance(x, (list, tuple, set, frozenset, dict)):
        return True
    return False


def _obj_spec(x: Any, include_private: bool, stringify_depth: int) -> Optional[dict]:
    """
    Non-builtin objects → {"type": fqname, "state": {...}}.
    depth == 0  : canonical snapshot via _snapshot_object
    depth >= 1  : enumerate __dict__/__slots__ and stringify one level
    """
    if getattr(type(x), "__module__", "") == "builtins":
        return None

    t = _qualtype(x)

    if stringify_depth <= 0:
        try:
            state0 = _snapshot_object(x, include_private=include_private)
        except Exception:
            state0 = {}
        return {"type": t, "state": state0}

    state: dict[str, Any] = {}
    try:
        processed: set[str] = set()

        # __dict__
        d = getattr(x, "__dict__", None)
        if isinstance(d, dict):
            for k, v in d.items():
                if not include_private and str(k).startswith("_"):
                    continue
                try:
                    if callable(v):
                        continue
                    state[str(k)] = _stringify_level1(v)
                    processed.add(str(k))
                except Exception:
                    pass

        # __slots__
        for name in _iter_slots(type(x)):
            if not include_private and str(name).startswith("_"):
                continue
            if str(name) in processed:
                continue
            try:
                v = getattr(x, name)
            except Exception:
                continue
            try:
                if callable(v):
                    continue
                state[str(name)] = _stringify_level1(v)
            except Exception:
                pass

    except Exception:
        state = {}

    return {"type": t, "state": state}


def _maybe_snapshot_self_before(args: tuple, snapshot_self: bool) -> Tuple[Optional[str], Optional[dict], Optional[dict]]:
    """Return (self_type, pub_before, all_before) or (None, None, None)."""
    if not (snapshot_self and len(args) >= 1):
        return None, None, None
    try:
        inst = args[0]
        self_type = _qualtype(inst)
        pub_before = _snapshot_object(inst, include_private=False)
        all_before = _snapshot_object(inst, include_private=True)
        return self_type, pub_before, all_before
    except Exception:
        return None, None, None


def _snapshot_self_after(args: tuple, had_pub_before: bool) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (pub_after, all_after) or (None, None)."""
    if not (had_pub_before and len(args) >= 1):
        return None, None
    try:
        pub_after = _snapshot_object(args[0], include_private=False)
        all_after = _snapshot_object(args[0], include_private=True)
        return pub_after, all_after
    except Exception:
        return None, None


def _stored_args_for(st: Any, drop_first: bool, args: tuple) -> tuple:
    """Persisted-args policy depending on storage format (Pickle vs JSON/REPR)."""
    if drop_first and len(args) >= 1:
        if isinstance(st, PickleStorage):
            return args[1:]
        # JSON/REPR: keep a literal-friendly placeholder
        return (repr(args[0]),) + args[1:]
    return args


def _build_obj_captures(args: tuple, kwargs: dict, drop_first: bool, policy: _TracePolicy) -> Tuple[Dict[int, dict], Dict[str, dict]]:
    """Build obj_args.pos/kw maps according to the capture policy."""
    obj_args_pos: Dict[int, dict] = {}
    obj_args_kw: Dict[str, dict] = {}
    if policy.capture_objects == "off":
        return obj_args_pos, obj_args_kw

    for idx, val in enumerate(args):
        if drop_first and idx == 0:
            continue
        if not _is_builtin_like(val):
            spec = _obj_spec(val, policy.include_private_objects, policy.objects_stringify_depth)
            if spec:
                obj_args_pos[idx] = spec

    for k, v in (kwargs or {}).items():
        if not _is_builtin_like(v):
            spec = _obj_spec(v, policy.include_private_objects, policy.objects_stringify_depth)
            if spec:
                obj_args_kw[str(k)] = spec

    return obj_args_pos, obj_args_kw


def _result_obj_spec_for(result: Any, policy: _TracePolicy) -> Optional[dict]:
    if policy.capture_objects == "off" or _is_builtin_like(result):
        return None
    return _obj_spec(result, policy.include_private_objects, policy.objects_stringify_depth)



def _emit_legacy_entry(
    st: Any, storage_path: Path, fullname: str, *,
    stored_args: tuple, kwargs: dict, result: Any,
    self_payload: Optional[dict], obj_args_pos: Dict[int, dict],
    obj_args_kw: Dict[str, dict], result_obj: Optional[dict],
) -> None:
    """Crée et sauvegarde une trace pour les anciens formats (pickle, json, repr)."""
    entry: Dict[str, Any] = {
        "trace_schema": "pytead/v1",
        "func": fullname,
        "args": stored_args,
        "kwargs": kwargs,
        "result": result,
        "timestamp": datetime.utcnow().isoformat(timespec="microseconds") + "Z",
    }
    if self_payload:
        entry["self"] = self_payload
    if obj_args_pos or obj_args_kw:
        entry["obj_args"] = {"pos": obj_args_pos, "kw": obj_args_kw}
    if result_obj:
        entry["result_obj"] = result_obj

    path = st.make_path(storage_path, fullname)
    st.dump(entry, path)

# ---------------------- Public decorator (refactored) ----------------------

# Thread-local pour éviter de capturer les appels imbriqués (seulement le "root" par pile)
_tlocal = threading.local()


def _now_iso() -> str:
    try:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    except Exception:
        return str(time.time())


def _depth_map() -> dict[int, int]:
    d = getattr(_tlocal, "depth", None)
    if d is None:
        d = {}
        _tlocal.depth = d
    return d


def _inc_depth(key: int) -> int:
    d = _depth_map()
    d[key] = d.get(key, 0) + 1
    return d[key]


def _dec_depth(key: int) -> int:
    d = _depth_map()
    cur = d.get(key, 0)
    if cur <= 1:
        d.pop(key, None)
        return 0
    d[key] = cur - 1
    return d[key]


def _fqn(func: Callable[..., Any]) -> str:
    mod = getattr(func, "__module__", "<unknown>")
    qn = getattr(func, "__qualname__", getattr(func, "__name__", "<?>"))
    return f"{mod}.{qn}"


def _make_entry_legacy(func_fqn: str, args: tuple, kwargs: dict, result: Any) -> dict:
    return {
        "trace_schema": "pytead/v1-state",
        "timestamp": _now_iso(),
        "func": func_fqn,
        "args": args,
        "kwargs": kwargs,
        "result": result,
    }


def _maybe_capture_graph(x: Any, *, max_depth: int, strict: str):
    # Local imports so the file header doesn't need to change if you don't use v2
    from .graph_capture import capture_object_graph
    if strict == "error":
        from .graph_capture import capture_object_graph_checked
        return capture_object_graph_checked(x, max_depth=max_depth)

    g = capture_object_graph(x, max_depth=max_depth)

    if strict == "warn":
        try:
            from .graph_utils import find_orphan_refs
            orphans = find_orphan_refs(g)
        except Exception:
            orphans = []
        if orphans:
            try:
                txt = ", ".join(f"{p} -> ref={rid}" for p, rid in orphans)
            except Exception:
                txt = "..."
            _logger.warning("capture produced orphan $ref(s): %s", txt)
    return g


def _make_entry_graph(func_fqn: str, args: tuple, kwargs: dict, result: Any, *, max_depth: int, strict: str) -> dict:
    """
    Capture **v2** (avec $id/$ref) pour chaque racine **séparément**.
    Important : on n'utilise PAS la projection v1 ici.
    """
    a_graph = [capture_object_graph_v2(a, max_depth=max_depth) for a in args]
    k_graph = {k: capture_object_graph_v2(v, max_depth=max_depth) for (k, v) in kwargs.items()}
    r_graph = capture_object_graph_v2(result, max_depth=max_depth)
    return {
        "trace_schema": "pytead/v2-graph",
        "timestamp": _now_iso(),
        "func": func_fqn,
        "args_graph": a_graph,
        "kwargs_graph": k_graph,
        "result_graph": r_graph,
    }



def trace(
    *,
    limit: int = 10,
    storage_dir: str | Path = "call_logs",
    storage: str | Any = "pickle",
    # Paramètres capture v2 (pour .gjson)
    max_depth: int = 5,
    strict_graph: str = "off",  # "off" | "warn" | "error"
    # Compat legacy (pickle/json/repr)
    capture_objects: str = "simple",          # "off" | "simple"
    include_private_objects: bool = False,
    objects_stringify_depth: int = 1,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Décorateur d’instrumentation.

    - Legacy storages (pickle/json/repr): enregistre
        - args/kwargs/result
        - bloc "self" pour les méthodes d'instance:
            * before/after : attributs publics uniquement
            * state_before/state_after : état complet (y compris privés)
        - "obj_args" (spécifs d'objets non-builtin dans args/kwargs)
        - "result_obj" si le résultat est un objet non-builtin
    - Graph JSON ('.gjson'): enregistre l'IR v2 (args_graph/kwargs_graph/result_graph)
      avec strict configurable.
    """
    # Résolution du backend
    if isinstance(storage, str):
        from .storage import get_storage
        st = get_storage(storage)
    else:
        st = storage

    storage_dir = Path(storage_dir)
    calls_done_by_func: dict[int, int] = {}

    def decorator(obj: Callable[..., Any]) -> Callable[..., Any]:
        # Détecter un descriptor staticmethod/classmethod et récupérer la vraie fonction
        desc_kind: str | None = None
        fn = obj
        if isinstance(obj, staticmethod):
            desc_kind = "staticmethod"
            fn = obj.__func__  # type: ignore[attr-defined]
        elif isinstance(obj, classmethod):
            desc_kind = "classmethod"
            fn = obj.__func__  # type: ignore[attr-defined]

        func_key = id(fn)
        func_fqn = _fqn(fn)
        try:
            sig = inspect.signature(fn)
        except Exception:
            sig = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            depth = _inc_depth(func_key)
            try:
                # Décider si on trace ce call
                should_trace = depth == 1 and calls_done_by_func.get(func_key, 0) < limit
                is_graph = getattr(st, "extension", "") == ".gjson"

                # --- PRE-SNAPSHOT pour legacy storages (besoin du "before") ---
                # deux flags distincts :
                # - snapshot_self : vrai uniquement pour méthodes d'instance
                # - drop_first    : on retire le 1er arg de la persistence pour instance **ou** classmethod
                snapshot_self = False
                drop_first = False
                self_type = None
                pub_before = None
                all_before = None
                stored_args = args
                obj_args_pos: Dict[int, dict] = {}
                obj_args_kw: Dict[str, dict] = {}
                policy = _TracePolicy(
                    limit=limit,
                    storage_dir=storage_dir,
                    storage=st,
                    capture_objects=capture_objects,
                    include_private_objects=include_private_objects,
                    objects_stringify_depth=objects_stringify_depth,
                )

                if should_trace and not is_graph:
                    # Heuristiques par signature + nature du descriptor
                    try:
                        params = list(sig.parameters.values()) if sig else []
                        is_instance_method = bool(params) and params[0].name == "self" and len(args) >= 1
                    except Exception:
                        is_instance_method = len(args) >= 1

                    is_class_method_call = (desc_kind == "classmethod" and len(args) >= 1)

                    snapshot_self = is_instance_method
                    drop_first = is_instance_method or is_class_method_call

                    # Snapshot 'self' avant appel (uniquement pour méthodes d'instance)
                    self_type, pub_before, all_before = _maybe_snapshot_self_before(
                        args, snapshot_self=snapshot_self
                    )

                    # Normalisation des args persistés (repr(self/cls) pour JSON/REPR, drop pour pickle)
                    stored_args = _stored_args_for(st, drop_first, args)

                    # Captures d'objets non-builtin dans args/kwargs (ignore aussi le 1er si drop_first)
                    obj_args_pos, obj_args_kw = _build_obj_captures(args, kwargs, drop_first, policy)

                # --- Appel utilisateur ---
                res = fn(*args, **kwargs)

                # --- Émission de l'entrée ---
                if should_trace:
                    n = calls_done_by_func.get(func_key, 0)
                    if is_graph:
                        entry = _make_entry_graph(
                            func_fqn, args, kwargs, res,
                            max_depth=max_depth, strict=strict_graph
                        )
                        path = st.make_path(storage_dir, func_fqn)
                        st.dump(entry, path)
                    else:
                        # Snapshot 'self' après appel (si instance method)
                        pub_after, all_after = _snapshot_self_after(
                            args, had_pub_before=(pub_before is not None)
                        )

                        self_payload = None
                        if snapshot_self and self_type is not None:
                            # before/after = publics ; state_* = état complet (incl. privés)
                            self_payload = {
                                "type": self_type,
                                "before": pub_before,
                                "after": pub_after,
                                "state_before": all_before if all_before is not None else pub_before,
                                "state_after": all_after if all_after is not None else pub_after,
                            }
                            # on conserve aussi les miroirs internes
                            if all_before is not None:
                                self_payload["_all_before"] = all_before
                            if all_after is not None:
                                self_payload["_all_after"] = all_after

                        result_obj = _result_obj_spec_for(res, policy)
                        _emit_legacy_entry(
                            st, storage_dir, func_fqn,
                            stored_args=stored_args,
                            kwargs=kwargs,
                            result=res,
                            self_payload=self_payload,
                            obj_args_pos=obj_args_pos,
                            obj_args_kw=obj_args_kw,
                            result_obj=result_obj,
                        )
                    calls_done_by_func[func_key] = n + 1

                return res
            except BaseException:
                raise
            finally:
                _dec_depth(func_key)

        # Ré-appliquer le même type de descriptor si nécessaire
        if desc_kind == "staticmethod":
            return staticmethod(wrapper)  # type: ignore[return-value]
        if desc_kind == "classmethod":
            return classmethod(wrapper)   # type: ignore[return-value]
        return wrapper

    return decorator

