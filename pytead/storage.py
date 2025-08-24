import ast
import json
import logging
import pickle
import pprint
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    extension = ".pkl"
    # (keep the atomic dump version only in your real file)
    def dump(self, entry: Dict[str, Any], path: Path) -> None:  # type: ignore[override]
        import os, tempfile

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", delete=False, dir=str(path.parent)
            ) as tmp:
                pickle.dump(entry, tmp)
                tmp_name = tmp.name
            os.replace(tmp_name, path)
        except Exception as exc:
            try:
                if "tmp_name" in locals():
                    os.unlink(tmp_name)
            except Exception:
                pass
            log.error("Failed to write pickle %s: %s", path, exc)

    def load(self, path: Path) -> Dict[str, Any]:  # type: ignore[override]
        with path.open("rb") as f:
            return pickle.load(f)


class JsonStorage(_BaseStorage):
    extension = ".json"

    def dump(self, entry: Dict[str, Any], path: Path) -> None:  # type: ignore[override]
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(
                    entry,
                    f,
                    ensure_ascii=False,
                    default=lambda o: o if json.dumps(o, default=str) else repr(o),
                )
        except Exception as exc:
            log.error("Failed to write json %s: %s", path, exc)

    def load(self, path: Path) -> Dict[str, Any]:  # type: ignore[override]
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("args"), list):
            data["args"] = tuple(data["args"])
        if data.get("kwargs") is None:
            data["kwargs"] = {}
        return data


class ReprStorage(_BaseStorage):
    extension = ".repr"

    def dump(self, entry: Dict[str, Any], path: Path) -> None:  # type: ignore[override]
        try:
            # 🔧 NEW: force a literal-friendly structure before pretty-printing
            lit = _to_literal(entry)
            txt = pprint.pformat(lit, width=100, sort_dicts=False)
            path.write_text(txt + "\n", encoding="utf-8")
        except Exception as exc:
            log.error("Failed to write repr %s: %s", path, exc)

    def load(self, path: Path) -> Dict[str, Any]:  # type: ignore[override]
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
