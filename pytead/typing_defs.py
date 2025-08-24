from __future__ import annotations
from typing import Any, Protocol, TypedDict
from pathlib import Path


class SelfSnapshot(TypedDict, total=False):
    type: str
    before: dict
    after: dict
    state_before: dict
    state_after: dict


class TraceEntry(TypedDict, total=False):
    trace_schema: str
    func: str
    args: tuple
    kwargs: dict
    result: Any
    timestamp: str
    self: SelfSnapshot


class StorageLike(Protocol):
    extension: str

    def make_path(self, storage_dir: Path, func_fullname: str):
        ...

    def dump(self, entry: dict, path: Path) -> None:
        ...

    def load(self, path: Path) -> dict:
        ...
