# pytead/testkit.py
from __future__ import annotations
from typing import Iterable, Tuple, Any, List, Union, Optional, Sequence
import os
from os import PathLike
from ._cases import case_id as _case_id
from .rt import (
    ensure_import_roots, resolve_attr, rehydrate,
    drop_self_placeholder, inject_object_args, assert_object_state,
)

__all__ = ["Case", "setup", "run_case", "param_ids"]

# Tuple schema used in generated tests
Case = Tuple[tuple, dict, Any, Optional[str], Optional[dict], Optional[dict], Optional[dict]]

def setup(here_file: Union[str, PathLike[str]], import_roots: Iterable[Union[str, PathLike[str]]]) -> None:
    """
    Prepare sys.path for generated tests. Relative paths are anchored on the
    project root (auto-détecté autour de `here_file`).
    """
    ensure_import_roots(here_file, import_roots)

def run_case(func_fq: str, case: Case) -> None:
    """
    Replay one recorded case and assert on result/object state.
    Case schema (7-tuple):
      (args, kwargs, expected, self_type, self_state, obj_args, result_spec)
    """
    args, kwargs, expected, self_type, self_state, obj_args, result_spec = case
    if self_type:
        inst = rehydrate(self_type, self_state)
        method_name = func_fq.rsplit(".", 1)[1]
        bound = getattr(inst, method_name)
        args = drop_self_placeholder(args, self_type)
        args, kwargs = inject_object_args(args, kwargs, obj_args, self_type)
        out = bound(*args, **kwargs)
    else:
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
    ids: List[str] = []
    for args, kwargs, *_ in cases:
        ids.append(_case_id(args, kwargs, maxlen=maxlen))
    return ids
