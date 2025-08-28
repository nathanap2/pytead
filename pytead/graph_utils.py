# pytead/graph_utils.py
from __future__ import annotations
from typing import Any, Iterable, Optional, List, Tuple, Literal
 
__all__ = [
    "collect_anchor_ids",
    "iter_bare_refs_with_paths",
    "find_orphan_refs",
    "find_local_orphan_refs",
    "find_id_paths",
    "validate_graph"
    ]


import logging

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

def project_v2_to_v1(
    node: Any,
    *,
    mode: Literal["capture","expected"] = "capture",
    donors_graphs: Iterable[Any] | None = None,
    tuples_as_lists: bool = False,
    warn_logger: Optional[logging.Logger] = None,
) -> Any:
    """
    Projette une IR v2 (avec $id/$list/$tuple/$set/$map) en "v1":
      - suppression systématique de $id,
      - dé-wrapping des listes/tuples (tuples→listes si tuples_as_lists=True),
      - conservation de {"$ref": N} (mode "capture"); on loggue un WARNING car en v1
        il n'existe plus d'ancres $id locales,
      - mode "expected": on part du principe que les refs externes ont été **inlinées**
        en amont; on conserve les refs restantes (s’il y en a, c’est une vraie anomalie
        et la génération lèvera après coup).
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

def find_orphan_refs(
    expected_graph: Any,
    donors_graphs: Iterable[Any] | None = None,
) -> list[tuple[str, int]]:
    """
    Renvoie la liste [(json_path, ref_id)] des {'$ref': N} présents dans
    `expected_graph` **dont N n'apparaît comme $id dans aucun des donneurs** (ni dans expected).
    Les donneurs typiques sont args_graph et kwargs_graph.
    """
    ids: set[int] = set()
    for g in donors_graphs or ():
        collect_anchor_ids(g, ids)
    # ancres internes de expected : légitimes
    collect_anchor_ids(expected_graph, ids)

    out: list[tuple[str, int]] = []
    for (p, rid) in iter_bare_refs_with_paths(expected_graph):
        if rid not in ids:
            out.append((p, rid))
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
    
    

class ProjectionRefError(RuntimeError):
    """Raised when a {'$ref': N} cannot be resolved during projection."""

def _scan_anchors(node: Any, index: Dict[int, Any]) -> None:
    """Collecte les ancres {'$id': N} dans un graphe v2."""
    if isinstance(node, dict):
        i = node.get("$id")
        if isinstance(i, int):
            index[i] = node
        for k, v in node.items():
            if k != "$id":
                _scan_anchors(v, index)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _scan_anchors(v, index)

def build_anchor_index(graphs: Iterable[Any]) -> Dict[int, Any]:
    """Construit un index {id -> ancre_dict} sur un ou plusieurs graphes."""
    idx: Dict[int, Any] = {}
    for g in graphs:
        _scan_anchors(g, idx)
    return idx

def inline_refs_from_donors(node: Any, donor_index: Dict[int, Any]) -> Any:
    """
    Retourne une *copie* de node où chaque feuille {'$ref': N} pointant vers une
    ancre présente dans donor_index est remplacée par une *copie profonde*
    de l'ancre correspondante (telle quelle, avec ses $id/$list internes).
    """
    if isinstance(node, dict):
        if set(node.keys()) == {"$ref"} and isinstance(node["$ref"], int):
            rid = node["$ref"]
            if rid in donor_index:
                return copy.deepcopy(donor_index[rid])
            return node
        # récursion
        return {k: inline_refs_from_donors(v, donor_index) for k, v in node.items()}
    elif isinstance(node, list):
        return [inline_refs_from_donors(x, donor_index) for x in node]
    elif isinstance(node, tuple):
        return tuple(inline_refs_from_donors(x, donor_index) for x in node)
    else:
        return node

def materialize_graph(
    root: Any,
    *,
    donor_graphs: Optional[Iterable[Any]] = None,
    normalize_nans: bool = True,
) -> Any:
    """
    Transforme un graphe v2 ($id/$ref/$list) en objet Python “plain”.
    - Résout *tous* les $ref via un index d’ancres (result_graph + donneurs éventuels).
    - Supprime les $id dans la sortie.
    - Remplace {'$list': [...]} par des listes Python.
    - Optionnellement, normalise NaN → None.
    """
    # 1) Index des ancres (result + donneurs)
    anchor_index: Dict[int, Any] = {}
    _scan_anchors(root, anchor_index)
    if donor_graphs:
        for g in donor_graphs:
            _scan_anchors(g, anchor_index)

    memo: Dict[int, Any] = {}

    def build(node: Any, path: str = "$") -> Any:
        # scalaire
        if not isinstance(node, (dict, list, tuple)):
            if normalize_nans and isinstance(node, float) and math.isnan(node):
                return None
            return node

        # séquences
        if isinstance(node, list):
            return [build(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if isinstance(node, tuple):
            return tuple(build(v, f"{path}[{i}]") for i, v in enumerate(node))

        # dicts
        # $ref leaf
        if set(node.keys()) == {"$ref"} and isinstance(node["$ref"], int):
            rid = node["$ref"]
            if rid in memo:
                return memo[rid]
            target = anchor_index.get(rid)
            if target is None:
                raise ProjectionRefError(f"path={path} ref={rid}")
            val = build(target, path=f"<id:{rid}>")
            memo[rid] = val
            return val

        # $list (avec ou sans $id)
        if "$list" in node and all(k in ("$id", "$list") for k in node.keys()):
            idv = node.get("$id")
            lst = node.get("$list", [])
            out = [build(v, f"{path}[{i}]") for i, v in enumerate(lst)]
            if isinstance(idv, int):
                memo[idv] = out
            return out

        # dict ordinaire (peut avoir $id)
        idv = node.get("$id")
        out = {k: build(v, f"{path}.{k}") for k, v in node.items() if k != "$id"}
        if isinstance(idv, int):
            memo[idv] = out
        return out

    return build(root)
