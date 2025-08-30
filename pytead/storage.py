import ast
import json
import logging
import pickle
import pprint
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Callable, IO

from .typing_defs import (
    StorageLike,
    TraceEntry,
    coerce_entry_shapes,
    basic_entry_invariants_ok,
)

from datetime import datetime
from dataclasses import asdict
from .errors import GraphJsonOrphanRef

log = logging.getLogger("pytead.storage")


def _is_scalar_literal(x: Any) -> bool:
    return isinstance(x, (str, int, float, bool, type(None)))


def _key_to_literal(k: Any) -> Any:
    if _is_scalar_literal(k):
        return k
    if isinstance(k, tuple):
        return tuple(_to_literal(x) for x in k)
    return repr(k)


def _to_literal(obj: Any) -> Any:
    """
    Convert an arbitrary object into a structure composed only
    of literal types (str, int, float, bool, None, list, tuple, dict).
    """
    if isinstance(obj, tuple):
        return tuple(_to_literal(x) for x in obj)
    if isinstance(obj, list):
        return [_to_literal(x) for x in obj]
    if isinstance(obj, dict):
        out: Dict[Any, Any] = {}
        for k, v in obj.items():
            out[_key_to_literal(k)] = _to_literal(v)
        return out
    if _is_scalar_literal(obj):
        return obj
    return repr(obj)




def _atomic_write(
    path: Path,
    *,
    mode: str,
    write_fn: Callable[[IO[Any]], None],
    open_kwargs: Optional[dict] = None,
) -> None:
    """
    Write to `path` atomically:
      - create parent directory,
      - write to a temporary file in the same directory,
      - flush + fsync,
      - os.replace onto the final path,
      - best-effort cleanup on failure.
    The caller provides `write_fn(tmp_file)` to perform the actual write.
    """
    import os, tempfile
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode,
            delete=False,
            dir=str(path.parent),
            **(open_kwargs or {})
        ) as tmp:
            write_fn(tmp)
            try:
                tmp.flush()
                os.fsync(tmp.fileno())
            except Exception:
                # On some FS (e.g., NFS, Windows text mode), fsync may not be necessary/available.
                pass
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    except Exception:
        try:
            if tmp_name:
                os.unlink(tmp_name)
        except Exception:
            pass
        raise


class _BaseStorage:
    extension = ""

    def make_path(self, storage_dir: Path, func_fullname: str) -> Path:
        prefix = func_fullname.replace(".", "_")
        filename = f"{prefix}__{uuid.uuid4().hex}{self.extension}"
        storage_dir.mkdir(parents=True, exist_ok=True)
        return storage_dir / filename


class PickleStorage(_BaseStorage):
    """Binary pickle-based storage with atomic writes."""
    extension = ".pkl"

    def dump(self, entry: Dict[str, Any], path: Path) -> None:  # type: ignore[override]
        """Serialize `entry` to `path` atomically using pickle."""
        try:
            _atomic_write(
                path,
                mode="wb",
                write_fn=lambda tmp: pickle.dump(entry, tmp, protocol=pickle.HIGHEST_PROTOCOL),
            )
        except Exception as exc:
            log.error("Failed to write pickle %s: %s", path, exc)

    def load(self, path: Path) -> Dict[str, Any]:  # type: ignore[override]
        """Load and return the pickled dict from `path`."""
        with path.open("rb") as f:
            return pickle.load(f)



class GraphJsonStorage(_BaseStorage):
    """
    Storage backend for the *graph-json* format.
    """

    extension = ".gjson"

    @staticmethod
    def _is_json_key_primitive(k: Any) -> bool:
        return isinstance(k, str)

    @classmethod
    def _make_json_key_safe(cls, obj: Any) -> Any:
        if isinstance(obj, dict):
            if all(cls._is_json_key_primitive(k) for k in obj.keys()):
                return {k: cls._make_json_key_safe(v) for k, v in obj.items()}
            items: list[list[Any]] = []
            for k, v in obj.items():
                kg = cls._make_json_key_safe(k)
                vg = cls._make_json_key_safe(v)
                items.append([kg, vg])
            items.sort(key=lambda kv: repr(kv[0]))
            return {"$map": items}
        if isinstance(obj, list):
            return [cls._make_json_key_safe(x) for x in obj]
        if isinstance(obj, tuple):
            return [cls._make_json_key_safe(x) for x in obj]
        return obj


    @staticmethod
    def _count_ids_refs(node: Any) -> tuple[int, int]:
        """Compte (#$id, #$ref) dans un graphe JSON-like (dict/list)."""
        ids = refs = 0
        def _walk(n: Any):
            nonlocal ids, refs
            if isinstance(n, dict):
                if set(n.keys()) == {"$ref"} and isinstance(n.get("$ref"), int):
                    refs += 1
                    return
                vid = n.get("$id")
                if isinstance(vid, int):
                    ids += 1
                # $map / $set : descente spécifique
                if isinstance(n.get("$map"), list):
                    for pair in n["$map"]:
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            _walk(pair[0]); _walk(pair[1])
                    # continuer vers autres clés tout de même
                if isinstance(n.get("$set"), list):
                    for e in n["$set"]:
                        _walk(e)
                for k, v in n.items():
                    if k in {"$id", "$map", "$set"}:
                        continue
                    _walk(v)
                return
            if isinstance(n, list):
                for x in n:
                    _walk(x)
        _walk(node)
        return ids, refs

    @staticmethod
    def _fmt_counts(label: str, node: Any) -> str:
        if node is None:
            return f"{label}=(absent)"
        i, r = GraphJsonStorage._count_ids_refs(node)
        hint = " v1-like (no $id)" if i == 0 else ""
        return f"{label}=(ids={i}, refs={r}{hint})"

    def dump(self, entry: Any, path: Path) -> None:
        """
        Serialize `entry` to JSON (UTF-8) atomically and enforce guardrails.
        """
        try:
            # Normalize input
            if hasattr(entry, "__dataclass_fields__"):
                data = asdict(entry)
            elif isinstance(entry, dict):
                data = dict(entry)
            else:
                log.warning("GraphJsonStorage.dump: unsupported entry type: %s", type(entry))
                return

            # Metadata
            data.setdefault("trace_schema", "pytead/anchored-graph")
            data.setdefault("timestamp", datetime.utcnow().isoformat(timespec="microseconds") + "Z")

            # -------- Guardrail: result_graph must be locally self-anchored --------
            rg = data.get("result_graph", None)
            if rg is not None:
                from .graph_utils import find_local_orphan_refs
                orphans = find_local_orphan_refs(rg)
                if orphans:
                    # Quick log for human scan
                    try:
                        log.warning(
                            "GraphJsonStorage guardrail: orphan ref(s) in result_graph: %s",
                            ", ".join(f"{p} -> ref={rid}" for (p, rid) in orphans),
                        )
                    except Exception:
                        pass

                    # Diagnostic résumé: comptes sur donneurs & résultat
                    args_g = data.get("args_graph")
                    kwargs_g = data.get("kwargs_graph")
                    diag = " | ".join([
                        self._fmt_counts("args", args_g),
                        self._fmt_counts("kwargs", kwargs_g),
                        self._fmt_counts("result", rg),
                    ])

                    # Message détaillé (format stable: 'path=… ref=N')
                    enriched = "; ".join(f"path={p} ref={rid}" for (p, rid) in orphans)
                    func_name = data.get("func") or "<unknown>"
                    raise GraphJsonOrphanRef(
                        f"graph-json guardrail: orphan $ref in result_graph: {enriched} "
                        f"(func={func_name}; {diag})"
                    )

            # JSON-safe dict keys without losing key types
            safe = self._make_json_key_safe(data)

            _atomic_write(
                path,
                mode="w",
                open_kwargs={"encoding": "utf-8"},
                write_fn=lambda tmp: json.dump(safe, tmp, ensure_ascii=False, indent=2),
            )

        except TypeError as exc:
            # Fallback: stringify all dict keys
            log.error(
                "GraphJsonStorage.dump: JSON serialization failed (%s). Falling back to stringified keys.",
                exc,
            )

            def _force_str_keys(o: Any) -> Any:
                if isinstance(o, dict):
                    return {str(k): _force_str_keys(v) for k, v in o.items()}
                if isinstance(o, (list, tuple)):
                    return [_force_str_keys(v) for v in o]
                return o

            fallback = _force_str_keys(entry if isinstance(entry, dict) else asdict(entry))
            _atomic_write(
                path,
                mode="w",
                open_kwargs={"encoding": "utf-8"},
                write_fn=lambda tmp: json.dump(fallback, tmp, ensure_ascii=False, indent=2),
            )

    def load(self, path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)




            
_REGISTRY: Dict[str, StorageLike] = {
    "pickle": PickleStorage(),
    "graph-json": GraphJsonStorage(),
}


def get_storage(name: Optional[str]) -> StorageLike:
    if not name:
        return _REGISTRY["pickle"]
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            "Unknown storage format '{}'. Available: {}".format(
                name, list(_REGISTRY.keys())
            )
        )


def storages_from_names(names: Optional[List[str]]) -> List[StorageLike]:
    if not names:
        return list(_REGISTRY.values())
    return [get_storage(n) for n in names]


def iter_entries(
    calls_dir: Path, formats: Optional[List[str]] = None
) -> Iterable[TraceEntry]:
    for st in storages_from_names(formats):
         for p in sorted(calls_dir.glob(f"*{st.extension}")):
             try:
                entry = st.load(p)
             except Exception as exc:
                 log.warning("Skipping corrupt trace %s: %s", p, exc)
                 continue
             # Normalize shapes (args tuple, kwargs dict) and run a cheap invariant gate
             entry = coerce_entry_shapes(entry)
             if not basic_entry_invariants_ok(entry):
                 log.warning("Skipping invalid trace %s", p)
                 continue
             yield entry  # Iterable[TraceEntry]
