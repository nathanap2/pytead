# pytead/graph_utils.py
from __future__ import annotations
from typing import Any, Iterable, Optional, List, Tuple, Literal



__all__ = [
    "collect_anchor_ids",
    "iter_bare_refs_with_paths",
    "find_orphan_refs_in_rendered",
    "find_local_orphan_refs",
    "find_id_paths",
    "validate_graph",
    "project_anchored_to_rendered",
]

import logging
from .normalize import sanitize_for_py_literals

_log_gc = logging.getLogger("pytead.graph_capture")



def find_id_paths(node: Any, target_id: int, path: str = "$") -> List[str]:
    """
    Retourne toutes les JSONPaths menant à un noeud qui porte `$id == target_id`.
    Couvre dict/list et les formes spéciales {"$map": ...}, {"$set": ...}.
    """
    found: List[str] = []
    if node is None:
        return found
    if isinstance(node, dict):
        v = node.get("$id")
        if isinstance(v, int) and v == target_id:
            found.append(path)
        # $map : pairs [k,v]
        if isinstance(node.get("$map"), list):
            for i, pair in enumerate(node["$map"]):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    found.extend(find_id_paths(pair[0], target_id, f"{path}.$map[{i}].key"))
                    found.extend(find_id_paths(pair[1], target_id, f"{path}.$map[{i}].value"))
        # $set : elements
        if isinstance(node.get("$set"), list):
            for i, e in enumerate(node["$set"]):
                found.extend(find_id_paths(e, target_id, f"{path}.$set[{i}]"))
        # other keys
        for k, v in node.items():
            if k in {"$id", "$map", "$set"}:
                continue
            found.extend(find_id_paths(v, target_id, f"{path}.{k}"))
        return found
    if isinstance(node, list):
        for i, e in enumerate(node):
            found.extend(find_id_paths(e, target_id, f"{path}[{i}]"))
    return found

def _collect_idmap(node: Any, out: dict[int, Any]) -> None:
    if isinstance(node, dict):
        vid = node.get("$id")
        if isinstance(vid, int):
            out[vid] = node
        for v in node.values():
            _collect_idmap(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect_idmap(v, out)

def _deepcopy_strip_ids(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _deepcopy_strip_ids(v) for k, v in node.items() if k != "$id"}
    if isinstance(node, list):
        return [_deepcopy_strip_ids(v) for v in node]
    if isinstance(node, tuple):
        return tuple(_deepcopy_strip_ids(v) for v in node)
    return node

def _unwrap_v2(node: Any, *, tuples_as_lists: bool) -> Any:
    """Dé-wrappe les formes v2 vers la 'surface' v1 (sans $id)."""
    if isinstance(node, dict):
        # formes spéciales
        if "$list" in node and isinstance(node["$list"], list):
            return [_unwrap_v2(x, tuples_as_lists=tuples_as_lists) for x in node["$list"]]
        if "$tuple" in node and isinstance(node["$tuple"], list):
            elems = [_unwrap_v2(x, tuples_as_lists=tuples_as_lists) for x in node["$tuple"]]
            return elems if tuples_as_lists else tuple(elems)
        if "$set" in node and isinstance(node["$set"], list):
            # v1 garde la forme 'marker' (pas de set Python côté snapshot)
            elems = [_unwrap_v2(x, tuples_as_lists=tuples_as_lists) for x in node["$set"]]
            return {"$set": elems, "$frozen": bool(node.get("$frozen"))}
        if "$map" in node and isinstance(node["$map"], list):
            pairs = []
            for pair in node["$map"]:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    k = _unwrap_v2(pair[0], tuples_as_lists=tuples_as_lists)
                    v = _unwrap_v2(pair[1], tuples_as_lists=tuples_as_lists)
                    pairs.append([k, v])
            return {"$map": pairs}
        # dict 'normal' (sans $id)
        return {k: _unwrap_v2(v, tuples_as_lists=tuples_as_lists)
                for k, v in node.items() if k != "$id"}

    if isinstance(node, list):
        return [_unwrap_v2(x, tuples_as_lists=tuples_as_lists) for x in node]
    if isinstance(node, tuple):
        elems = [_unwrap_v2(x, tuples_as_lists=tuples_as_lists) for x in node]
        return elems if tuples_as_lists else tuple(elems)
    return node

def project_anchored_to_rendered(
    node: Any,
    *,
    mode: Literal["capture","expected"] = "capture",
    donors_graphs: Iterable[Any] | None = None,
    tuples_as_lists: bool = False,
    warn_logger: Optional[logging.Logger] = None,
) -> Any:
    """
    Projection **Anchored → Rendered** :
      - supprime systématiquement les `$id`,
      - dé-wrappe `$list`/`$tuple`/`$set`/`$map` (tuples→listes si `tuples_as_lists=True`),
      - en mode `"capture"`, conserve `{"$ref": N}` (warning possible si plus d’ancre locale),
      - en mode `"expected"`, on suppose les refs **externes** déjà inlinées ; toute ref restante
        sera signalée par la génération / validation en aval.
    """

    def _dec(n: Any) -> Any:
        # $ref : garder tel quel (les orphelines seront signalées par les tests/outils)
        if isinstance(n, dict) and set(n.keys()) == {"$ref"}:
            if warn_logger and mode == "capture":
                try:
                    warn_logger.warning(
                        "Emitting $ref=%s without a surviving '$id' anchor in v1 projection",
                        n["$ref"],
                    )
                except Exception:
                    pass
            return {"$ref": n["$ref"]}

        # $map : [(k_graph, v_graph)] (sans $id)
        if isinstance(n, dict) and "$map" in n:
            pairs = n.get("$map") or []
            out = []
            for kv in pairs:
                if isinstance(kv, (list, tuple)) and len(kv) == 2:
                    k, v = kv
                    out.append([_dec(k), _dec(v)])
            return {"$map": out}

        # $set : liste triée + $frozen (sans $id)
        if isinstance(n, dict) and "$set" in n:
            elems = [_dec(x) for x in n.get("$set") or []]
            return {"$set": elems, "$frozen": bool(n.get("$frozen", False))}

        # $list
        if isinstance(n, dict) and "$list" in n:
            return [_dec(x) for x in n.get("$list") or []]

        # $tuple
        if isinstance(n, dict) and "$tuple" in n:
            items = [_dec(x) for x in n.get("$tuple") or []]
            return items if tuples_as_lists else tuple(items)

        # dict "objet" v2 (avec $id + attributs)
        if isinstance(n, dict):
            # strip $id, puis descente
            return {k: _dec(v) for (k, v) in n.items() if k != "$id"}

        # liste Python brute (ex: après descente)
        if isinstance(n, list):
            return [_dec(x) for x in n]

        return n

    return _dec(node)



def collect_anchor_ids(node, ids=None):
    """
    Parcourt un graphe et collecte tous les $id (y compris dans $map/$set).
    Retourne un set[int].
    """
    if ids is None:
        ids = set()
    if node is None:
        return ids
    if isinstance(node, dict):
        v = node.get("$id")
        if isinstance(v, int):
            ids.add(v)

        # $map : liste de paires [k_graph, v_graph]
        if isinstance(node.get("$map"), list):
            for pair in node["$map"]:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    collect_anchor_ids(pair[0], ids)
                    collect_anchor_ids(pair[1], ids)

        # $set : liste d’éléments
        if isinstance(node.get("$set"), list):
            for e in node["$set"]:
                collect_anchor_ids(e, ids)

        # autres clés (en évitant de repasser dans $map/$set)
        for k, v in node.items():
            if k in {"$id", "$map", "$set"}:
                continue
            collect_anchor_ids(v, ids)

    elif isinstance(node, list):
        for e in node:
            collect_anchor_ids(e, ids)

    return ids


def iter_bare_refs_with_paths(node, path: str = "$"):
    """
    Itère sur toutes les références 'pures' {'$ref': N} et yield (json_path, N).
    Couvre dict/list ainsi que les formes spéciales {"$map": ...} et {"$set": ...}.
    """
    if node is None:
        return

    if isinstance(node, dict):
        # cas ref isolée
        if set(node.keys()) == {"$ref"} and isinstance(node.get("$ref"), int):
            yield (path, int(node["$ref"]))
            return

        # $map
        if isinstance(node.get("$map"), list):
            for i, pair in enumerate(node["$map"]):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    yield from iter_bare_refs_with_paths(pair[0], f"{path}.$map[{i}].key")
                    yield from iter_bare_refs_with_paths(pair[1], f"{path}.$map[{i}].value")

        # $set
        if isinstance(node.get("$set"), list):
            for i, e in enumerate(node["$set"]):
                yield from iter_bare_refs_with_paths(e, f"{path}.$set[{i}]")

        # autres clés
        for k, v in node.items():
            if k in {"$id", "$map", "$set"}:
                continue
            yield from iter_bare_refs_with_paths(v, f"{path}.{k}")
        return

    if isinstance(node, list):
        for i, e in enumerate(node):
            yield from iter_bare_refs_with_paths(e, f"{path}[{i}]")

def find_orphan_refs_in_rendered(
    expected_graph: Any,
    donors_graphs: Iterable[Any] | None = None,
) -> list[tuple[str, int]]:
    """
    Return a list of (json_path, ref_id) for every {"$ref": N} found in a *rendered graph*
    (i.e., a graph where "$id" anchors were stripped) that does not have a corresponding
    "$id" anchor either in the rendered graph itself or in any of the donor graphs.

    Parameters
    ----------
    expected_graph : Any
        The rendered graph to validate (typically the "expected" snapshot after projection).
        By convention rendered graphs should not carry "$id" anchors; this function is robust
        if they do.
    donors_graphs : Iterable[Any] | None
        Optional graphs that may contain anchors to satisfy references present in the rendered
        graph. In practice, these are usually the *anchored* `args_graph` and `kwargs_graph`
        captured at trace time. If donors are already rendered, they simply won't contribute
        any anchors.

    Returns
    -------
    list[tuple[str, int]]
        A list of (json_path, ref_id) pairs, one for each orphan reference. `json_path` is a
        stable JSONPath-like string pointing to the exact location of the `{"$ref": N}` node.

    Notes
    -----
    - This is a *pure* check: it does not mutate its inputs.
    - Traversal covers plain dict/list as well as special shapes: {"$map": [...]}, {"$set": [...]}
      produced by the anchored -> rendered projection.
    - Complexity is O(size(graphs)).

    Examples
    --------
    If expected_graph contains {"base": {"$ref": 3}} and no donor provides an anchor
    node with "$id": 3, the function returns [("$.base", 3)].
    """
    ids: set[int] = set()

    # Collect anchors from donors first (args/kwargs/self graphs captured in anchored form)
    for g in donors_graphs or ():
        collect_anchor_ids(g, ids)

    # Also consider anchors that might still be present inside the rendered graph
    collect_anchor_ids(expected_graph, ids)

    # Any bare ref whose id is not in the collected anchors is an orphan
    out: list[tuple[str, int]] = []
    for (path, rid) in iter_bare_refs_with_paths(expected_graph):
        if rid not in ids:
            out.append((path, rid))
    return out


def validate_graph(graph: Any) -> List[str]:
    """
    Validation légère: signale (messages texte) les situations "dangereuses".
    - $ref sans $id correspondant dans le même graphe
    (on pourra ajouter d’autres règles progressivement).
    """
    msgs: List[str] = []
    ids = collect_anchor_ids(graph)
    for (p, rid) in iter_bare_refs_with_paths(graph):
        if rid not in ids:
            msgs.append(f"orphan-ref: path={p} ref={rid}")
    return msgs
    
    

def _build_ref_donor_index(graphs: list[Any]) -> dict[int, Any]:
    """
    Build an {id -> anchor_node} index from a list of donor graphs (args/kwargs/self).
    Donors may be nested lists/dicts/tuples; we index every `{"$id": int}` we find.
    """
    index: dict[int, Any] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "$id" in node and isinstance(node["$id"], int):
                index[node["$id"]] = node
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    for g in graphs:
        _walk(g)
    return index



def _inline_external_refs_in_expected(expected_graph: Any, donor_index: dict[int, Any]) -> Any:
    """
    Inline external {'$ref': N} found in the *expected* graph using anchors from donor_index.
    - If N is defined inside `expected_graph` itself, we keep the ref (internal aliasing).
    - If N is only defined in donors, we replace the ref by a deep copy of the donor anchor
      with its `$id` stripped (and recurse).
    - If N is unknown everywhere, we leave it as-is (the runtime guard will fail the test).
    """
    # Collect internal ids (anchors) present inside expected
    internal_ids: set[int] = set()

    def _collect_ids(node: Any) -> None:
        if isinstance(node, dict):
            if "$id" in node and isinstance(node["$id"], int):
                internal_ids.add(node["$id"])
            for v in node.values():
                _collect_ids(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _collect_ids(v)

    _collect_ids(expected_graph)

    # Deepcopy + strip `$id`
    def _strip_ids(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _strip_ids(v) for k, v in node.items() if k != "$id"}
        if isinstance(node, list):
            return [_strip_ids(v) for v in node]
        if isinstance(node, tuple):
            return [_strip_ids(v) for v in node]
        return node

    def _copy(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: _copy(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_copy(v) for v in node]
        if isinstance(node, tuple):
            return [_copy(v) for v in node]
        return node

    def _inline(node: Any) -> Any:
        if isinstance(node, dict):
            if set(node.keys()) == {"$ref"} and isinstance(node.get("$ref"), int):
                rid = node["$ref"]
                if rid not in internal_ids and rid in donor_index:
                    return _inline(_strip_ids(_copy(donor_index[rid])))
                return node
            return {k: _inline(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [_inline(v) for v in node]
        return node

    return _inline(expected_graph)
    
def find_local_orphan_refs(graph: Any) -> List[Tuple[str, int]]:
    """
    Version structurée de `validate_graph`: renvoie la liste des (json_path, ref_id)
    pour lesquels un `{'$ref': N}` n'a **pas** d'ancre `$id` dans *le même* graphe.
    (Indépendant d'éventuels donneurs: args/kwargs ne comptent pas ici.)
    """
    out: List[Tuple[str, int]] = []
    ids = collect_anchor_ids(graph)
    for (p, rid) in iter_bare_refs_with_paths(graph):
        if rid not in ids:
            out.append((p, rid))
    return out
    
    

def inline_and_project_expected(
    entry: dict,
    *,
    func_qualname: str = "",
    tuples_as_lists: bool = True,
) -> Any:
    """
    Construire l'expected 'lisible' à partir d'une trace v2 :
      1) Inline des {'$ref': N} *externes* (via ancres dans args/kwargs),
      2) Vérifie les refs orphelines (compte tenu des donneurs),
      3) Projette v2→v1 (strip $id; tuples→listes si demandé),
      4) Sanitize NaN/±Inf → None.
    Le wording des logs/erreurs est préservé (compat tests).
    """
    log = logging.getLogger("pytead.gen")

    # Cas legacy (pas de graphe résultat) : renvoie la valeur normalisée telle quelle
    if "result_graph" not in entry or entry.get("result_graph") is None:
        return sanitize_for_py_literals(entry.get("result"))

    args_graph = entry.get("args_graph") or []
    kwargs_graph = entry.get("kwargs_graph") or {}
    result_graph = entry.get("result_graph")

    # (1) Inline des refs externes à expected grâce aux ancres des donneurs
    donor_index = _build_ref_donor_index([args_graph, kwargs_graph])
    inlined = _inline_external_refs_in_expected(result_graph, donor_index)

    # (2) Orphelines après inlining (donneurs = args/kwargs; expected compte aussi)
    orphans = find_orphan_refs_in_rendered(inlined, donors_graphs=[args_graph, kwargs_graph])
    if orphans:
        try:
            txt = ", ".join(f"{p} -> ref={rid}" for p, rid in orphans)
            log.warning(
                "ORPHAN_REF remains after projection for %s: %d orphan(s): %s",
                func_qualname or "<unknown>", len(orphans), txt
            )
        except Exception:
            pass
        from .errors import OrphanRefInExpected  # import local pour éviter les cycles
        details = "; ".join(f"path={p} ref={rid}" for p, rid in orphans)
        raise OrphanRefInExpected(
            "Unresolved {'$ref': N} in expected snapshot after projection. "
            f"Found {len(orphans)} orphan ref(s): {details}"
        )

    # (3) Dé-aliasser toutes les refs (internes ET externes) *avant* projection,
    #     en reproduisant exactement l’ancienne sémantique (cycle-safe).
    anchor: dict[int, Any] = {}
    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            rid = node.get("$id")
            if isinstance(rid, int):
                anchor[rid] = node
            for v in node.values():
                _collect(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _collect(v)
    _collect(args_graph); _collect(kwargs_graph); _collect(inlined)

    INPROGRESS = object()
    memo: dict[int, Any] = {}
    def _resolve_ref_id(rid: int, path: str) -> Any:
        if rid in memo:
            val = memo[rid]
            return {} if val is INPROGRESS else val
        tgt = anchor.get(rid)
        memo[rid] = INPROGRESS
        val = _mat(tgt, path)
        memo[rid] = val
        return val

    def _mat(node: Any, path: str = "$") -> Any:
        if isinstance(node, dict):
            # ref pure
            if set(node.keys()) == {"$ref"} and isinstance(node["$ref"], int):
                return _resolve_ref_id(node["$ref"], path)
            # $list (forme v2)
            if "$list" in node and isinstance(node["$list"], list):
                return [_mat(x, f"{path}[{i}]") for i, x in enumerate(node["$list"])]
            # dict “normal” : strip $id et descente
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("$id", "$list"):
                    continue
                child = f"{path}.{k}" if path != "$" else f"$.{k}"
                out[k] = _mat(v, child)
            return out
        if isinstance(node, list):
            return [_mat(x, f"{path}[{i}]") for i, x in enumerate(node)]
        if isinstance(node, tuple):
            return [_mat(x, f"{path}[{i}]") for i, x in enumerate(node)]
        return node

    expanded = _mat(inlined, "$")

    # (4) Projection
    projected = project_anchored_to_rendered(expanded, mode="expected", tuples_as_lists=tuples_as_lists)

    # (5) Normalisation littéraux Python
    return sanitize_for_py_literals(projected)
