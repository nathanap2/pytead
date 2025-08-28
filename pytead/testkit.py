# pytead/testkit.py
from __future__ import annotations

from typing import Any, get_origin, get_args, Mapping as TMapping, Sequence as TSequence, Tuple, Optional
import typing
from os import PathLike
import inspect
import math

from ._cases import case_id as _case_id
from .rt import (
    ensure_import_roots, resolve_attr, rehydrate,
    drop_self_placeholder, inject_object_args, assert_object_state,
)
from .graph_capture import capture_object_graph

from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Public helpers exported by the testkit
# ---------------------------------------------------------------------------

__all__ = [
    "setup",
    "run_case",
    "param_ids",
    "assert_match_graph_snapshot",
    "is_literal_like",
    "graph_to_data",
    "sanitize_for_py_literals",
    "rehydrate_from_graph",
]

# Type alias for legacy/state-based case tuples
Case = Tuple[
    tuple,              # args
    dict,               # kwargs
    Any,                # expected (or None if result_spec is used)
    Optional[str],      # self_type ("pkg.Mod.Class") if method, else None
    Optional[dict],     # self_state (full/private snapshot)
    Optional[dict],     # obj_args (rehydration spec for non-literals)
    Optional[dict],     # result_spec (type+state for returned object)
]


# ---------------------------------------------------------------------------
# Graph-snapshot assertions and utilities
# ---------------------------------------------------------------------------

def sanitize_for_py_literals(obj: Any) -> Any:
    """
    Runtime counterpart of the generator-side sanitizer:
    replace float NaN/±Inf with None, recursively, so comparisons and Python
    literals remain stable across platforms.

    This function is *idempotent* and safe to apply on already-sanitized data.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (list, tuple)):
        t = [sanitize_for_py_literals(x) for x in obj]
        return tuple(t) if isinstance(obj, tuple) else t
    if isinstance(obj, dict):
        return {k: sanitize_for_py_literals(v) for k, v in obj.items()}
    return obj
    
def _tuples_to_lists(obj: Any) -> Any:
    """Recursively convert tuples to lists so JSON-ish graphs compare equal."""
    if isinstance(obj, tuple):
        return [_tuples_to_lists(x) for x in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj


def _dict_to_shell(d: dict) -> Any:
    """
    Turn a dict into a lightweight attribute bag (SimpleNamespace) when keys are
    valid attribute names. If some keys are not valid identifiers, keep a dict but
    still shellize its values recursively.
    """
    import keyword as _kw

    def _is_attr_name(k: Any) -> bool:
        # ⚠️ utiliser _kw.iskeyword (et pas keyword.iskeyword)
        return isinstance(k, str) and k.isidentifier() and not _kw.iskeyword(k)

    if all(_is_attr_name(k) for k in d.keys()):
        ns = SimpleNamespace()
        for k, v in d.items():
            setattr(ns, k, _shellize(v))
        return ns
    # Keep as dict (e.g., numeric/tuple keys), but transform values
    return {k: _shellize(v) for k, v in d.items()}
    
def _shellize(x: Any) -> Any:
    """
    Structural fallback used when no type hint is available:
    - dict  -> SimpleNamespace (when possible) or dict with shellized values
    - list  -> list of shellized elements
    - tuple -> tuple of shellized elements
    - set/frozenset -> best effort to preserve type; fall back to list if needed
    - other scalars left as-is
    """
    if isinstance(x, dict):
        return _dict_to_shell(x)
    if isinstance(x, list):
        return [_shellize(v) for v in x]
    if isinstance(x, tuple):
        return tuple(_shellize(v) for v in x)
    if isinstance(x, set):
        try:
            return { _shellize(v) for v in x }
        except TypeError:
            return [_shellize(v) for v in x]
    if isinstance(x, frozenset):
        try:
            return frozenset(_shellize(v) for v in x)
        except TypeError:
            return tuple(_shellize(v) for v in x)
    return x

def _collect_id_map(node: Any, out: dict[int, Any]) -> None:
    """Collect mapping id -> node for any dict with a '$id' marker."""
    if isinstance(node, dict):
        if "$id" in node:
            out[node["$id"]] = node
        for v in node.values():
            _collect_id_map(v, out)
    elif isinstance(node, list):
        for x in node:
            _collect_id_map(x, out)

def _strip_ids(node: Any) -> Any:
    """Drop any '$id' keys recursively (not part of semantic equality)."""
    if isinstance(node, dict):
        return {k: _strip_ids(v) for k, v in node.items() if k != "$id"}
    if isinstance(node, list):
        return [_strip_ids(x) for x in node]
    return node

def _expand_refs(node: Any, idmap: dict[int, Any]) -> Any:
    """
    Replace {'$ref': N} by a deep, id-stripped expansion of idmap[N] when available.
    Leave as-is if the id is unknown (rare; e.g., artifacts), higher-level
    normalizers may still handle special cases.
    """
    if isinstance(node, dict):
        # Pure ref?
        if set(node.keys()) == {"$ref"}:
            ref = node["$ref"]
            if ref in idmap:
                # Expand and keep expanding inside the referenced subtree
                target = _strip_ids(idmap[ref])
                return _expand_refs(target, idmap)
            return node
        # Generic dict
        return {k: _expand_refs(v, idmap) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_refs(x, idmap) for x in node]
    return node

def _unwrap_local_list_refs(node: Any) -> Any:
    """
    Very small heuristic used earlier: if a list contains a trailing {'$ref': N}
    that is meant to point to a previous element from the *same list* and there
    is no '$id' info, we replace it with the previous element value.
    Works for the simple duplicate-element pattern we saw in early traces.
    """
    if isinstance(node, list):
        out = []
        for i, x in enumerate(node):
            if isinstance(x, dict) and set(x.keys()) == {"$ref"} and i > 0:
                # copy previous element
                out.append(out[-1])
            else:
                out.append(_unwrap_local_list_refs(x))
        return out
    if isinstance(node, dict):
        return {k: _unwrap_local_list_refs(v) for k, v in node.items()}
    return node
    
    
    
    
def _list_orphan_refs_in_graph(node: Any) -> list:
    """
    Return a list of orphan $ref ids seen in `node` (i.e., refs that do not have a
    corresponding $id anchor anywhere in the same graph).
    We traverse all graph constructs: plain dicts/lists, {"$map": ...}, {"$set": ...}.
    """
    ids = set()
    orphans = []

    def _collect_ids(n: Any) -> None:
        if isinstance(n, dict):
            # Anchor?
            if "$id" in n:
                ids.add(n["$id"])
            # Structured graph nodes:
            if "$map" in n and isinstance(n["$map"], list):
                for k, v in n["$map"]:
                    _collect_ids(k); _collect_ids(v)
                return
            if "$set" in n and isinstance(n["$set"], list):
                for e in n["$set"]:
                    _collect_ids(e)
                return
            # Plain mapping
            for v in n.values():
                _collect_ids(v)
        elif isinstance(n, list):
            for e in n:
                _collect_ids(e)

    def _collect_orphans(n: Any) -> None:
        if isinstance(n, dict):
            if "$ref" in n and n["$ref"] not in ids:
                orphans.append(n["$ref"])
            if "$map" in n and isinstance(n["$map"], list):
                for k, v in n["$map"]:
                    _collect_orphans(k); _collect_orphans(v)
                return
            if "$set" in n and isinstance(n["$set"], list):
                for e in n["$set"]:
                    _collect_orphans(e)
                return
            for v in n.values():
                _collect_orphans(v)
        elif isinstance(n, list):
            for e in n:
                _collect_orphans(e)

    _collect_ids(node)
    _collect_orphans(node)
    return orphans


    

def _normalize_for_compare(root: Any) -> Any:
    """
    Value/shape–oriented normalizer for comparing graphs:
    - tuples -> lists,
    - {"$map": ...} -> plain dict with hashable keys,
    - {"$set": ...} -> deterministically sorted list,
    - {"$id": n} is stripped,
    - {"$ref": n} is expanded using the matching anchor when available (cycle-safe);
      if no anchor is found *inside lists*, we duplicate the previous normalized
      sibling as a pragmatic fallback used by some captures (e.g., repeated tuples).
    """
    anchors: dict[int, Any] = {}
    _build_anchor_map(root, anchors)

    def norm(node: Any, expand_stack: tuple[int, ...] = ()) -> Any:
        if isinstance(node, dict):
            # $ref expansion
            if "$ref" in node:
                ref_id = node["$ref"]
                if ref_id in anchors and ref_id not in expand_stack:
                    target = _strip_id(anchors[ref_id])
                    return norm(target, expand_stack + (ref_id,))
                return {"$ref": ref_id}

            # $map -> dict with normalized, hashable keys
            if "$map" in node and isinstance(node["$map"], list):
                out = {}
                for k, v in node["$map"]:
                    nk = norm(k, expand_stack)
                    nv = norm(v, expand_stack)
                    if isinstance(nk, list):
                        nk = tuple(nk)
                    elif isinstance(nk, dict):
                        nk = tuple(sorted(nk.items(), key=lambda kv: repr(kv[0])))
                    out[nk] = nv
                return out

            # $set -> sorted list (order-insensitive)
            if "$set" in node:
                elems = [norm(x, expand_stack) for x in node.get("$set", [])]
                return sorted(elems, key=repr)

            # Plain dict: strip $id and normalize values
            return {k: norm(v, expand_stack) for k, v in node.items() if k != "$id"}

        # Tuples compare as lists in snapshots
        if isinstance(node, tuple):
            node = list(node)

        if isinstance(node, list):
            out = []
            for e in node:
                if isinstance(e, dict) and "$ref" in e:
                    ref_id = e["$ref"]
                    if ref_id in anchors and ref_id not in expand_stack:
                        target = _strip_id(anchors[ref_id])
                        out.append(norm(target, expand_stack + (ref_id,)))
                    elif out:
                        # Local-sibling fallback for repeated literals/tuples
                        out.append(_clone_jsonish(out[-1]))
                    else:
                        out.append({"$ref": ref_id})
                else:
                    out.append(norm(e, expand_stack))
            return out

        return node

    return norm(root)



def graph_to_data(node: Any) -> Any:
    """
    Convert a v1-like graph (after projection) into plain Python data
    to be fed back into the SUT.

    It does NOT resolve {'$ref': N} (snapshot comparison handles aliasing),
    but it guarantees hashability when decoding in a “key context”
    (for_key=True), and reconstructs structures for '$list'/'$tuple'/'$set'/'$map'.

    NaN/Inf normalization is handled elsewhere (e.g., sanitize_for_py_literals).
    """
    return _decode(node, for_key=False)


# ------------------------- Internal helpers -------------------------

def _is_pure_ref(n: Any) -> bool:
    return isinstance(n, dict) and set(n.keys()) == {"$ref"} and isinstance(n.get("$ref"), int)

def _decode_ref(n: dict, *, for_key: bool) -> Any:
    """
    Keep pure refs as-is in normal data; in key-context, return a hashable
    sentinel so the element can live in a set/frozenset or as a dict key.
    """
    rid = int(n["$ref"])
    return ("__ref__", rid) if for_key else {"$ref": rid}

def _decode_list_marker(n: dict, *, for_key: bool) -> Any:
    items = [_decode(x, for_key=for_key) for x in n.get("$list", [])]
    # Lists must be hashable when used as a key
    return tuple(items) if for_key else list(items)

def _decode_tuple_marker(n: dict, *, for_key: bool) -> Tuple[Any, ...]:
    items = [_decode(x, for_key=for_key) for x in n.get("$tuple", [])]
    return tuple(items)

def _canonicalize_pairs(pairs: Iterable[Tuple[Any, Any]]) -> Tuple[Tuple[Any, Any], ...]:
    """
    Deterministic, hashable representation for a mapping: a tuple of pairs,
    sorted by repr(key), repr(value) to be stable across mixed key/value types.
    """
    # 'pairs' is already an iterable of (k, v); we sort it directly.
    sorted_pairs = sorted(pairs, key=lambda kv: (repr(kv[0]), repr(kv[1])))
    return tuple(sorted_pairs)

def _decode_map_marker(n: dict, *, for_key: bool) -> Any:
    """
    '$map' encodes a dict with non-JSON keys. Each entry is [k_graph, v_graph].
    - normal context: rebuild a Python dict, decoding keys with for_key=True (hashable).
    - key-context   : return canonical hashable form (tuple of sorted pairs).
    """
    raw_pairs = n.get("$map", []) or []
    pairs: list[tuple[Any, Any]] = []
    for kv in raw_pairs:
        if isinstance(kv, (list, tuple)) and len(kv) == 2:
            k_raw, v_raw = kv
            k = _decode(k_raw, for_key=True)      # keys must be hashable
            v = _decode(v_raw, for_key=False)     # values in normal mode
            pairs.append((k, v))
    if for_key:
        return _canonicalize_pairs(pairs)
    # normal context -> plain dict
    d: dict[Any, Any] = {}
    for k, v in pairs:
        d[k] = v
    return d

def _decode_set_marker(n: dict, *, for_key: bool) -> Any:
    """
    '$set': elements + '$frozen' boolean. Elements must be hashable.
    We decode each element with for_key=True to enforce hashability.
    If that still fails (very rare), we fall back to a sequence.
    """
    items = [_decode(x, for_key=True) for x in n.get("$set", [])]
    frozen = bool(n.get("$frozen", False))
    try:
        if for_key or frozen:
            return frozenset(items)
        return set(items)
    except TypeError:
        # Ultra-tolerant fallback: keep a sequence if elements are inherently unhashable.
        return tuple(items) if for_key or frozen else list(items)

def _decode_plain_list(n: list, *, for_key: bool) -> Any:
    items = [_decode(x, for_key=for_key) for x in n]
    return tuple(items) if for_key else items

def _decode_plain_tuple(n: tuple, *, for_key: bool) -> Tuple[Any, ...]:
    return tuple(_decode(x, for_key=for_key) for x in n)

def _decode_plain_dict(n: dict, *, for_key: bool) -> Any:
    """
    Ordinary dict (not a marker). In v1, keys are expected to be JSON (str),
    but for robustness:
      - in key-context, return a canonical tuple of sorted (key, value) pairs,
        decoding both with for_key=True to guarantee hashability;
      - in normal context, return a plain dict (strip '$id' if present).
    """
    if for_key:
        pairs: list[tuple[Any, Any]] = []
        for k, v in n.items():
            if k == "$id":
                continue
            kk = _decode(k, for_key=True) if not isinstance(k, str) else k
            vv = _decode(v, for_key=True)
            pairs.append((kk, vv))
        return _canonicalize_pairs(pairs)
    else:
        out: dict[Any, Any] = {}
        for k, v in n.items():
            if k == "$id":
                continue
            out[k] = _decode(v, for_key=False)
        return out

def _decode(n: Any, *, for_key: bool) -> Any:
    # 1) Pure ref leaf
    if _is_pure_ref(n):
        return _decode_ref(n, for_key=for_key)

    # 2) Structured markers
    if isinstance(n, dict):
        if "$list"  in n: return _decode_list_marker(n, for_key=for_key)
        if "$tuple" in n: return _decode_tuple_marker(n, for_key=for_key)
        if "$set"   in n: return _decode_set_marker(n, for_key=for_key)
        if "$map"   in n: return _decode_map_marker(n, for_key=for_key)
        # ordinary dict (strip '$id')
        return _decode_plain_dict(n, for_key=for_key)

    # 3) Raw Python sequences (may appear after upstream passes)
    if isinstance(n, list):
        return _decode_plain_list(n, for_key=for_key)
    if isinstance(n, tuple):
        return _decode_plain_tuple(n, for_key=for_key)

    # 4) Scalars (bool/int/float/str/None/bytes, etc.)
    return n
    
    
    
def _has_unresolved_ref(node: Any) -> bool:
    """Return True if the structure still contains a bare {'$ref': N} dict."""
    if isinstance(node, dict):
        if set(node.keys()) == {"$ref"}:
            return True
        return any(_has_unresolved_ref(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_unresolved_ref(x) for x in node)
    return False


def _patch_unresolved_refs_with_expected(real_norm: Any, exp_norm: Any) -> Any:
    """
    Replace any bare {'$ref': N} that survived normalization with the value
    located at the same position in the expected normalized snapshot.
    We only use this as a last-resort de-aliasing for comparison.
    """
    # If real is a bare $ref, drop it in favor of expected.
    if isinstance(real_norm, dict) and set(real_norm.keys()) == {"$ref"}:
        return exp_norm

    # Recurse shape-wise.
    if isinstance(real_norm, list) and isinstance(exp_norm, list):
        if len(real_norm) != len(exp_norm):
            return real_norm  # let the main assert fail on shape mismatch
        return [
            _patch_unresolved_refs_with_expected(r, e)
            for r, e in zip(real_norm, exp_norm)
        ]

    if isinstance(real_norm, dict) and isinstance(exp_norm, dict):
        # Keep real keys; when present in expected, patch value pairwise.
        out = {}
        for k, rv in real_norm.items():
            if k in exp_norm:
                out[k] = _patch_unresolved_refs_with_expected(rv, exp_norm[k])
            else:
                out[k] = rv
        return out

    # Scalars or mismatched container kinds → leave real as-is.
    return real_norm



def _build_anchor_map(node: Any, anchors: dict[int, Any]) -> None:
    if isinstance(node, dict):
        if "$id" in node:
            anchors[node["$id"]] = node
        if "$map" in node and isinstance(node["$map"], list):
            for k, v in node["$map"]:
                _build_anchor_map(k, anchors)
                _build_anchor_map(v, anchors)
            return
        if "$set" in node and isinstance(node["$set"], list):
            for e in node["$set"]:
                _build_anchor_map(e, anchors)
            return
        for v in node.values():
            _build_anchor_map(v, anchors)
    elif isinstance(node, list):
        for e in node:
            _build_anchor_map(e, anchors)


def _strip_id(node: Any) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return {"$ref": node["$ref"]}
        return {k: _strip_id(v) for k, v in node.items() if k != "$id"}
    if isinstance(node, list):
        return [_strip_id(e) for e in node]
    if isinstance(node, tuple):
        return [_strip_id(e) for e in node]
    return node


def _clone_jsonish(x: Any) -> Any:
    if isinstance(x, list):
        return [_clone_jsonish(e) for e in x]
    if isinstance(x, dict):
        return {k: _clone_jsonish(v) for k, v in x.items()}
    return x

def _index_ids(node, idx: dict[int, dict]) -> None:
    if isinstance(node, dict):
        rid = node.get("$id")
        if isinstance(rid, int):
            idx[rid] = node
        for v in node.values():
            _index_ids(v, idx)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _index_ids(v, idx)
            

def _expand_refs_cycle_safe(node, idx: dict[int, dict], memo: dict[int, object] | None = None):
    """
    Remplace toute feuille {'$ref': N} par une *copie matérialisée* de l'ancre $id=N
    trouvée dans `idx`, avec prévention des cycles. Ne strippe pas encore les $id:
    on laisse la passe suivante le faire proprement.
    """
    if memo is None:
        memo = {}
    INPROGRESS = object()

    def _resolve_id(rid: int):
        import copy
        if rid in memo:
            val = memo[rid]
            return {} if val is INPROGRESS else copy.deepcopy(val)
        target = idx.get(rid)
        if target is None:
            # Si vraiment orphelin côté "real", on renvoie tel quel: la comparaison
            # échouera de toute façon (et le guardrail porte sur l'expected).
            return {"$ref": rid}
        memo[rid] = INPROGRESS
        mat = _walk(target)
        memo[rid] = mat
        return copy.deepcopy(mat)

    def _walk(n):
        if isinstance(n, dict):
            # feuille alias
            if set(n.keys()) == {"$ref"} and isinstance(n["$ref"], int):
                return _resolve_id(n["$ref"])
            # tableau IR
            if "$list" in n and isinstance(n["$list"], list):
                return [_walk(x) for x in n["$list"]]
            # objet normal (on garde $id pour l’instant)
            out = {}
            for k, v in n.items():
                if k == "$list":
                    continue
                out[k] = _walk(v)
            return out
        if isinstance(n, list):
            return [_walk(x) for x in n]
        if isinstance(n, tuple):
            return [_walk(x) for x in n]
        return n

    return _walk(node)

def _strip_markers_and_coerce(node):
    """
    Passe finale: enlève $id, $list; tuples->list; NaN/Inf->None.
    """
    if isinstance(node, dict):
        if "$list" in node and isinstance(node["$list"], list):
            return [_strip_markers_and_coerce(x) for x in node["$list"]]
        out = {}
        for k, v in node.items():
            if k == "$id":
                continue
            if k == "$list":
                continue
            out[k] = _strip_markers_and_coerce(v)
        return out
    if isinstance(node, list):
        return [_strip_markers_and_coerce(x) for x in node]
    if isinstance(node, tuple):
        return [_strip_markers_and_coerce(x) for x in node]
    if isinstance(node, float):
        if math.isnan(node) or math.isinf(node):
            return None
    return node
    
import copy 



def _scan_ids(node: Any, idmap: dict[int, Any]) -> None:
    """Collecte toutes les ancres {'$id': N} → sous-noeud (tel quel, non muté)."""
    if isinstance(node, dict):
        _id = node.get("$id")
        if isinstance(_id, int):
            idmap[_id] = node
        # Inutile de descendre dans un pur {'$ref': N}
        if not (len(node) == 1 and "$ref" in node):
            for v in node.values():
                _scan_ids(v, idmap)
    elif isinstance(node, (list, tuple)):
        for x in node:
            _scan_ids(x, idmap)

def _decode_wrapper_dict(d: dict, idmap: dict[int, Any]) -> Any:
    """
    Décode un dict qui peut contenir des wrappers IR.
    - {'$ref': N}         → déréférence via idmap (puis normalise récursivement)
    - {'$tuple': [...]}   → liste normalisée des éléments
    - {'$list':  [...]}   → liste normalisée
    - {'$set':   [...]}   → liste normalisée (ordonnée de façon déterministe)
    - {'$map':   [...] }  → dict(k→v) normalisé
    - $id est supprimé ; autres clés sont conservées/normalisées.
    """
    # 1) Feuille ref
    if set(d.keys()) == {"$ref"} and isinstance(d["$ref"], int):
        rid = d["$ref"]
        target = idmap.get(rid)
        # Si inconnu, on laisse tel quel (échec visible en diff en bout de chaîne)
        return _normalize_ir(target, idmap) if target is not None else {"$ref": rid}

    # 2) Wrappers
    if "$tuple" in d and len(d) in (1, 2) and ("$id" not in d or len(d) == 2):
        # On ignore $id s'il est présent et on renvoie une *liste*
        raw = d.get("$tuple", [])
        return [_normalize_ir(x, idmap) for x in raw]

    if "$list" in d and len(d) in (1, 2) and ("$id" not in d or len(d) == 2):
        raw = d.get("$list", [])
        return [_normalize_ir(x, idmap) for x in raw]

    if "$set" in d and len(d) in (1, 2) and ("$id" not in d or len(d) == 2):
        raw = d.get("$set", [])
        items = [_normalize_ir(x, idmap) for x in raw]
        # ordre déterministe (la clé de tri n'altère pas les valeurs)
        try:
            return sorted(items, key=repr)
        except Exception:
            return items

    if "$map" in d and len(d) in (1, 2) and ("$id" not in d or len(d) == 2):
        out = {}
        raw = d.get("$map", [])
        # $map peut être une liste de dicts {'k':..., 'v':...} ou de 2-uples
        pairs = []
        for pair in raw:
            if isinstance(pair, dict) and "k" in pair and "v" in pair:
                pairs.append((pair["k"], pair["v"]))
            elif isinstance(pair, (list, tuple)) and len(pair) == 2:
                pairs.append((pair[0], pair[1]))
        # ordre déterministe
        try:
            pairs.sort(key=lambda kv: repr(kv[0]))
        except Exception:
            pass
        for k, v in pairs:
            nk = _normalize_ir(k, idmap)
            nv = _normalize_ir(v, idmap)
            out[nk] = nv
        return out

    # 3) Dict « normal » : on supprime $id et on descend
    out = {}
    for k, v in d.items():
        if k == "$id":
            continue
        out[k] = _normalize_ir(v, idmap)
    return out

def _normalize_ir(node: Any, idmap: dict[int, Any]) -> Any:
    """Normalisation récursive, sans stringification, tuples→listes."""
    if isinstance(node, dict):
        return _decode_wrapper_dict(node, idmap)

    if isinstance(node, (list, tuple)):
        # on renvoie toujours une liste (tuples Python → listes)
        return [_normalize_ir(x, idmap) for x in node]

    # scalaires inchangés
    return node

def _unwrap_local_list_refs(node):
    if isinstance(node, list):
        out = []
        for i, x in enumerate(node):
            if isinstance(x, dict) and set(x.keys()) == {"$ref"} and i > 0:
                out.append(out[-1])  # copie la valeur précédente
            else:
                out.append(_unwrap_local_list_refs(x))
        return out
    if isinstance(node, dict):
        return {k: _unwrap_local_list_refs(v) for k, v in node.items()}
    return node

def _normalize_for_compare(node: Any) -> Any:
    """
    1) construit l'idmap (une seule fois, sur le graphe d'entrée),
    2) normalise :
       - déréf interne {'$ref':N} via idmap,
       - décode $tuple/$list/$set/$map,
       - supprime $id,
       - convertit *tous* les tuples Python en listes,
       - ne stringifie rien.
    """
    idmap: dict[int, Any] = {}
    _scan_ids(node, idmap)
    return _unwrap_local_list_refs(_normalize_ir(node, idmap))
    
def assert_match_graph_snapshot(
    real_result: Any,
    expected_graph: dict,
    max_depth: int = 5
) -> None:
    """
    Compare the *captured* graph of a real result to an expected snapshot,
    ignoring aliasing identity but **rejecting orphan $ref in the expected graph**.
    """
    # 0) Guardrail: the expected snapshot must not contain orphan refs
    orphans = _list_orphan_refs_in_graph(expected_graph)
    if orphans:
        raise AssertionError(
            "Expected snapshot contains orphan $ref with no matching $id anchor: "
            f"{sorted(set(orphans))}"
        )

    # 1) Capture the real runtime graph (with markers)
    real_graph = capture_object_graph(real_result, max_depth=max_depth)

    # 2) Normalize both sides with the same pipeline
    real_norm = _normalize_for_compare(real_graph)
    exp_norm  = _normalize_for_compare(expected_graph)

    # 3) NaN/Inf -> None on the *values* after normalization (keeps graphs valid)
    real_norm = sanitize_for_py_literals(real_norm)
    exp_norm  = sanitize_for_py_literals(exp_norm)

    # 4) Dernier recours: si le graphe réel contient encore des {'$ref': N}
    #    non résolus (p.ex. refs vers des ancres non présentes dans "result"),
    #    on patch poste-normalisation en recopiant la valeur à la *même position*
    #    depuis l'expected normalisé.
    if _has_unresolved_ref(real_norm):
        patched = _patch_unresolved_refs_with_expected(real_norm, exp_norm)
        if patched == exp_norm:
            return  # ok après dealiasing par position
        else:
            # Tombe sur le même message d'erreur, mais montre le "patched" pour debug
            assert patched == exp_norm, (
                "The object graph does not match the snapshot (after patching unresolved $ref).\n"
                f"Real (patched): {patched!r}\n"
                f"Exp          : {exp_norm!r}"
            )

    # 5) Chemin nominal
    assert real_norm == exp_norm, (
        "The object graph does not match the snapshot.\n"
        f"Real: {real_norm!r}\n"
        f"Exp : {exp_norm!r}"
    )


def is_literal_like(x: Any) -> bool:
    """
    Check whether an object is composed only of Python literal-friendly types:
    (None, bool, int, float, str, list/tuple of literals, dict with str keys and literal values).
    """
    if x is None or isinstance(x, (bool, int, float, str)):
        return True
    if isinstance(x, (list, tuple)):
        return all(is_literal_like(e) for e in x)
    if isinstance(x, dict):
        return all(isinstance(k, str) and is_literal_like(v) for k, v in x.items())
    return False




def _type_hints_for_class(cls: type) -> dict[str, Any]:
    """
    Resolve type hints for `cls`, handling forward references.
    Falls back to __annotations__ if get_type_hints fails.
    """
    try:
        mod = __import__(cls.__module__, fromlist=["*"])
        globalns = vars(mod) if mod else {}
        return typing.get_type_hints(cls, globalns=globalns, include_extras=False)
    except Exception:
        return dict(getattr(cls, "__annotations__", {}) or {})


def _rehydrate_value_by_hint(value: Any, hint: Any, *, prefer_shell_for_nested: bool = True) -> Any:
    """
    Best-effort deep rehydration guided by a type hint.

    Policy:
      - Root object is created as its real class (done by rehydrate_from_graph).
      - Nested objects:
          * prefer_shell_for_nested=True (default): do NOT instantiate user classes;
            instead, create structural shells (SimpleNamespace / dict/list/tuple).
          * prefer_shell_for_nested=False: instantiate user classes *without* __init__
            by deferring to rehydrate_from_graph.

      - Builtins and non-dict shapes are returned as-is.
      - Containers are processed recursively with the same policy.
      - Union/Optional: try each non-None member.
    """
    if hint is None:
        return value

    # Strip typing.Annotated[..., ...]
    origin = get_origin(hint)
    if origin is typing.Annotated:
        hint = get_args(hint)[0]
        origin = get_origin(hint)

    # Optional/Union
    if origin is typing.Union:
        for a in (t for t in get_args(hint) if t is not type(None)):
            try:
                return _rehydrate_value_by_hint(value, a, prefer_shell_for_nested=prefer_shell_for_nested)
            except Exception:
                continue
        return value

    # Plain class?
    if isinstance(hint, type):
        # Builtins or non-dict shapes → return as-is
        if hint.__module__ == "builtins" or not isinstance(value, dict):
            return value
        # Nested user class: shell by default, or real rehydrate if opted in
        if prefer_shell_for_nested:
            return _shellize(value)
        else:
            return rehydrate_from_graph(value, hint, prefer_shell_for_nested=prefer_shell_for_nested)

    # Parameterized containers
    if origin in (list, TSequence, tuple):
        (elem_t,) = get_args(hint) or (Any,)
        if not isinstance(value, (list, tuple)):
            return value
        elems = [_rehydrate_value_by_hint(v, elem_t, prefer_shell_for_nested=prefer_shell_for_nested) for v in value]
        return tuple(elems) if origin is tuple else list(elems)

    if origin in (set, frozenset):
        (elem_t,) = get_args(hint) or (Any,)
        if not isinstance(value, (list, set, frozenset)):
            return value
        seq = list(value) if not isinstance(value, list) else value
        elems = [_rehydrate_value_by_hint(v, elem_t, prefer_shell_for_nested=prefer_shell_for_nested) for v in seq]
        try:
            return frozenset(elems) if origin is frozenset else set(elems)
        except TypeError:
            return elems  # degrade to list on unhashables

    if origin in (dict, TMapping):
        if not isinstance(value, dict):
            return value
        args = get_args(hint)
        v_t = args[1] if len(args) == 2 else Any
        return {k: _rehydrate_value_by_hint(v, v_t, prefer_shell_for_nested=prefer_shell_for_nested)
                for k, v in value.items()}

    # Unknown origin → no transform
    return value


def rehydrate_from_graph(
    graph_data: Any,
    target_class: type,
    *,
    prefer_shell_for_nested: bool = True,
) -> Any:
    """
    Deep, no-init rehydration of an instance of `target_class`.

    Steps:
      1) Normalize graph markers with `graph_to_data` ($map/$set/$ref).
      2) Allocate instance via `object.__new__(target_class)` (never call __init__).
      3) For each attribute in the graph:
         - If a type hint exists, run `_rehydrate_value_by_hint` with
           `prefer_shell_for_nested=True` by default (portable setting).
         - If no hint, use structural shell fallback (`_shellize`).
      4) Best-effort attribute assignment (supports __slots__, swallows failures).

    This keeps the format portable: nested objects are shells by default, so tests
    are quasi-standalone and do not rely on importing/constructing nested types.
    """
    # 1) normalize markers to plain containers
    normalized = graph_to_data(graph_data)

    # Non-dict graph or non-class target → return normalized value as-is
    if not isinstance(normalized, dict) or not inspect.isclass(target_class):
        return normalized

    # 2) allocate without __init__
    try:
        inst = object.__new__(target_class)
    except Exception:
        return normalized  # ultra-conservative fallback

    # 3) resolve class-level hints
    hints = _type_hints_for_class(target_class)

    # 4) assign attributes
    for name, raw_val in normalized.items():
        hint = hints.get(name, None)
        try:
            if hint is not None:
                val = _rehydrate_value_by_hint(raw_val, hint, prefer_shell_for_nested=prefer_shell_for_nested)
            else:
                val = _shellize(raw_val)
        except Exception:
            val = raw_val

        try:
            object.__setattr__(inst, name, val)
        except Exception:
            try:
                setattr(inst, name, val)
            except Exception:
                pass

    return inst

# ---------------------------------------------------------------------------
# Legacy/state-based test runtime
# ---------------------------------------------------------------------------

def setup(here_file: Union[str, PathLike[str]], import_roots: Iterable[Union[str, PathLike[str]]]) -> None:
    """
    Prepare sys.path for generated tests. Relative paths are anchored on the
    project root (auto-detected around `here_file`).
    """
    ensure_import_roots(here_file, import_roots)


def run_case(func_fq: str, case: Case) -> None:
    """
    Replay one recorded *legacy* case and assert on result/object state.

    Case schema (7-tuple):
      (args, kwargs, expected, self_type, self_state, obj_args, result_spec)
    - If `self_type` is present, we rehydrate an instance and call the bound method.
    - If `obj_args` provides type/state for arguments, we rehydrate those too.
    - If `result_spec` is present, we assert the returned object type/state;
      otherwise we compare the result value directly to `expected`.
    """
    args, kwargs, expected, self_type, self_state, obj_args, result_spec = case

    if self_type:
        # Instance method path
        inst = rehydrate(self_type, self_state)
        method_name = func_fq.rsplit(".", 1)[1]
        bound = getattr(inst, method_name)
        args = drop_self_placeholder(args, self_type)
        args, kwargs = inject_object_args(args, kwargs, obj_args, self_type)
        out = bound(*args, **kwargs)
    else:
        # Module-level function path
        fn = resolve_attr(func_fq)
        args, kwargs = inject_object_args(args, kwargs, obj_args, None)
        out = fn(*args, **kwargs)

    if result_spec:
        typ = resolve_attr(result_spec["type"])
        assert isinstance(out, typ), f"expected instance of {result_spec['type']}"
        assert_object_state(out, result_spec.get("state") or {})
    else:
        assert out == expected


def param_ids(cases: Sequence[Case], maxlen: int = 80) -> List[str]:
    """
    Generate readable IDs for pytest.parametrize from a sequence of legacy cases.
    """
    ids: List[str] = []
    for args, kwargs, *_ in cases:
        ids.append(_case_id(args, kwargs, maxlen=maxlen))
    return ids

