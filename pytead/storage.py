import ast
import json
import logging
import pickle
import pprint
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Callable, IO

from .typing_defs import StorageLike, TraceEntry

from datetime import datetime
from dataclasses import asdict

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

def _json_default(o: Any) -> Any:
    try:
        json.dumps(o)
        return o
    except Exception:
        return repr(o)


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



class JsonStorage(_BaseStorage):
    """UTF-8 JSON storage with atomic writes and light normalization."""
    extension = ".json"

    def dump(self, entry: Dict[str, Any], path: Path) -> None:  # type: ignore[override]
        """Serialize `entry` to `path` atomically as JSON (utf-8)."""
        try:
            _atomic_write(
                path,
                mode="w",
                open_kwargs={"encoding": "utf-8"},
                write_fn=lambda tmp: json.dump(entry, tmp, ensure_ascii=False, default=_json_default),
            )
        except Exception as exc:
            log.error("Failed to write json %s: %s", path, exc)

    def load(self, path: Path) -> Dict[str, Any]:  # type: ignore[override]
        """
        Load and return the JSON dict from `path`.

        Normalization:
        - Converts list-based "args" to tuple (if present),
        - Replaces missing/None "kwargs" by {} for downstream stability.
        """
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # Normalize for downstream code expecting tuple args / dict kwargs
        if isinstance(data.get("args"), list):
            try:
                data["args"] = tuple(data["args"])
            except Exception:
                # Best-effort: keep as-is if conversion fails
                pass
        if data.get("kwargs") is None:
            data["kwargs"] = {}

        return data

class ReprStorage(_BaseStorage):
    extension = ".repr"

    def dump(self, entry: Dict[str, Any], path: Path) -> None:  # atomique
        import os, tempfile
        tmp_name = None
        try:
            lit = _to_literal(entry)
            txt = pprint.pformat(lit, width=100, sort_dicts=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
                tmp.write(txt + "\n")
                tmp_name = tmp.name
            os.replace(tmp_name, path)
        except Exception as exc:
            try:
                if tmp_name:
                    os.unlink(tmp_name)
            except Exception:
                pass
            log.error("Failed to write repr %s: %s", path, exc)

    def load(self, path: Path) -> Dict[str, Any]:  # <-- RESTAURÉ
        txt = path.read_text(encoding="utf-8")
        data = ast.literal_eval(txt)
        return data
        
        
class GraphJsonStorage(_BaseStorage):
    """
    Storage backend for the *graph-json* format.

    Goals
    -----
    - Persist traces as JSON while keeping object *data graphs* fully serializable.
    - Preserve the type of dictionary *keys* (ints, tuples, etc.) without
      silently stringifying them: only `str` keys are stored as a native JSON
      object; any non-`str` key switches the encoding to a stable `{"$map": ...}`
      representation.
    - Keep the on-disk shape deterministic (sorted when needed) to avoid noisy diffs.

    Key design
    ----------
    * Dictionaries:
        - If **all keys are `str`**, keep a regular JSON object: `{ "k": ... }`.
        - Otherwise, encode as: `{ "$map": [[key_graph, value_graph], ...] }`
          where each element is recursively processed. The `$map` list is sorted
          by `repr(key_graph)` for deterministic ordering.
    * Tuples are encoded as JSON lists (round-trips are handled by the testkit).
    * Sets should already be normalized upstream; if they appear, they would need
      a marker shape like `{"$set": [...], "$frozen": bool}` (handled elsewhere).
    * We do **not** attempt to change NaN/Inf here (Python's `json` allows them);
      the generator/testkit sanitize to `None` when embedding as Python literals.

    Fallback
    --------
    If JSON serialization still fails (e.g. due to an exotic object sneaking in),
    we fall back to a conservative strategy that **stringifies all dict keys**
    recursively, so the write never aborts. This fallback is not expected under
    normal operation because graphs are pre-normalized.
    """

    extension = ".gjson"  # single suffix so Path(...).suffix == ".gjson"

    @staticmethod
    def _is_json_key_primitive(k: Any) -> bool:
        """
        Only `str` keys are allowed to stay as a plain JSON object.

        Rationale: allowing numbers/bools as keys would *appear* to work in JSON
        because Python would string-convert on dump/load, but that loses types.
        By restricting to `str` here, we preserve non-string keys via `$map`.
        """
        return isinstance(k, str)

    @classmethod
    def _make_json_key_safe(cls, obj: Any) -> Any:
        """
        Recursively transform `obj` so that *dictionary keys* are JSON-safe
        without losing key types.

        - Dict with all-`str` keys       -> keep a plain JSON object.
        - Dict with any non-`str` key    -> encode as {"$map": [[k_graph, v_graph], ...]}.
        - Lists/Tuples                   -> recurse into elements; tuples become lists.
        - Everything else                -> returned as-is (already JSON-compatible or
                                            expected to have been normalized upstream).
        """
        if isinstance(obj, dict):
            # All keys are str -> keep native JSON object, preserve keys exactly.
            if all(cls._is_json_key_primitive(k) for k in obj.keys()):
                return {k: cls._make_json_key_safe(v) for k, v in obj.items()}

            # Heterogeneous / non-string keys → encode as a list of pairs.
            items: list[list[Any]] = []
            for k, v in obj.items():
                kg = cls._make_json_key_safe(k)
                vg = cls._make_json_key_safe(v)
                items.append([kg, vg])
            # Deterministic order: sort by repr of the captured key graph.
            items.sort(key=lambda kv: repr(kv[0]))
            return {"$map": items}

        if isinstance(obj, list):
            return [cls._make_json_key_safe(x) for x in obj]

        if isinstance(obj, tuple):
            # JSON doesn't have tuples; use a list.
            return [cls._make_json_key_safe(x) for x in obj]

        # Sets (if any) should already be transformed by the capture layer.
        # Scalars/strings/bools/None/etc. are fine as-is.
        return obj

    def dump(self, entry: Any, path: Path) -> None:
        """
        Serialize `entry` to `path` atomically as UTF-8 JSON.

        Accepts either:
          - a dataclass instance (auto-converted with `asdict`), or
          - a plain dict.

        Adds:
          - `trace_schema`: "pytead/v1-graph" (idempotent),
          - `timestamp`   : ISO-8601 UTC with microseconds + 'Z'.
        """
        from dataclasses import asdict
        from datetime import datetime

        try:
            # Normalize `entry` to a dict.
            if hasattr(entry, "__dataclass_fields__"):
                data = asdict(entry)
            elif isinstance(entry, dict):
                data = dict(entry)
            else:
                log.warning("GraphJsonStorage.dump: unsupported entry type: %s", type(entry))
                return

            # Ensure schema/timestamp fields exist.
            data.setdefault("trace_schema", "pytead/v1-graph")
            data.setdefault("timestamp", datetime.utcnow().isoformat(timespec="microseconds") + "Z")

            # Ensure dictionary keys are JSON-safe *without* losing types.
            safe = self._make_json_key_safe(data)

            _atomic_write(
                path,
                mode="w",
                open_kwargs={"encoding": "utf-8"},
                write_fn=lambda tmp: json.dump(
                    safe, tmp, ensure_ascii=False, indent=2
                ),
            )

        except TypeError as exc:
            # Ultra-conservative fallback: stringify *all* dict keys everywhere.
            # This should be rare; it trades type fidelity for guaranteed write.
            log.error(
                "GraphJsonStorage.dump: JSON serialization failed (%s). "
                "Falling back to stringified keys.", exc
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
        """
        Load and return the JSON dict from `path` (UTF-8).

        Note:
        - The structure may contain the `$map` encoding for non-string dict keys.
          The testkit provides `graph_to_data(...)` to convert it back when needed.
        """
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)




            
_REGISTRY: Dict[str, StorageLike] = {
    "pickle": PickleStorage(),
    "json": JsonStorage(),
    "repr": ReprStorage(),
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
            if "func" not in entry:
                log.warning("Skipping trace without 'func': %s", p)
                continue
            args = entry.get("args", ())
            if not isinstance(args, tuple):
                try:
                    args = tuple(args)
                except Exception:
                    pass
            entry["args"] = args  # type: ignore[index]
            entry["kwargs"] = entry.get("kwargs", {}) or {}  # type: ignore[index]
            yield entry  # type: ignore[misc]
