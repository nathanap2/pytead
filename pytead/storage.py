import ast
import json
import logging
import pickle
import pprint
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Callable, IO

from .typing_defs import StorageLike, TraceEntry

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
_REGISTRY: Dict[str, StorageLike] = {
    "pickle": PickleStorage(),
    "json": JsonStorage(),
    "repr": ReprStorage(),
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
