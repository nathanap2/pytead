# pytead/gen_types.py
from __future__ import annotations
from typing import (
    Any,
    Mapping as TMapping,
    Sequence as TSequence,
    Union,
    Optional,
    get_origin,
    get_args,
)
import inspect
import importlib
import re
from dataclasses import dataclass
from collections import defaultdict

# ------------------ Lisibilité / politiques ------------------

# 1) Union : si on observe trop de variantes, on retombe sur Any
MAX_UNION = 6

# 2) Repr opaques du style "<pkg.Class object at 0x...>" -> Any
_OBJ_REPR_RE = re.compile(r"^<(?P<qual>[\w\.]+)\s+object at 0x[0-9A-Fa-f]+>$")

# 3) Alias de noms pour un rendu plus court
NAME_ALIASES = {
    "collections.abc.Mapping": "Mapping",
    "collections.abc.MutableMapping": "MutableMapping",
    "collections.abc.Sequence": "Sequence",
    "collections.abc.MutableSequence": "MutableSequence",
    "collections.abc.Set": "Set",
    "collections.abc.FrozenSet": "FrozenSet",
    "collections.abc.Iterable": "Iterable",
}

# ------------------ Helpers typage de valeurs ------------------


def _is_bool(x: Any) -> bool:
    return isinstance(x, bool)


def _typeof(x: Any) -> type:
    return bool if _is_bool(x) else type(x)


def _flatten_union_types(tp: Any) -> list[Any]:
    """Retourne la liste des constituants d'une union, aplatie et *sans* récursivité."""
    if get_origin(tp) is Union:
        out: list[Any] = []
        for a in get_args(tp):
            if get_origin(a) is Union:
                out.extend(get_args(a))
            else:
                out.append(a)
        return out
    return [tp]


def _dedup_types(types_seq: list[Any]) -> list[Any]:
    """Déduplique par nom lisible pour garder un set stable."""
    seen = set()
    uniq = []
    for t in types_seq:
        key = getattr(t, "__name__", str(t))
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


def _merge(a: Any, b: Any) -> Any:
    """Fusionne deux types candidats en une union compacte, plafonnée."""
    if a == b:
        return a
    # Généralisation douce numérique
    if a in {int, float} and b in {int, float}:
        return Union[int, float]
    # Union aplatie + dédupliquée
    cand = _flatten_union_types(a) + _flatten_union_types(b)
    uniq = _dedup_types(cand)
    if len(uniq) > MAX_UNION:
        return Any
    return Union[tuple(uniq)]


def _merge_seq(seq: Any) -> Any:
    inner = None
    for x in list(seq):
        tx = infer_type(x)
        inner = tx if inner is None else _merge(inner, tx)
    if isinstance(seq, list):
        return list[inner or Any]
    if isinstance(seq, tuple):
        return TSequence[inner or Any]
    return TSequence[inner or Any]


def _merge_mapping(d: dict[Any, Any]) -> Any:
    if not d:
        return TMapping[Any, Any]
    tk = tv = None
    for k, v in d.items():
        tk = infer_type(k) if tk is None else _merge(tk, infer_type(k))
        tv = infer_type(v) if tv is None else _merge(tv, infer_type(v))
    # Clés purement str -> str
    if tk == str or (get_origin(tk) is Union and set(get_args(tk)) == {str}):
        tk = str
    return TMapping[tk, tv]


def infer_type(x: Any) -> Any:
    """Inférence prudente : au moindre doute → Any (mais on garde la structure des conteneurs)."""
    if x is None:
        return type(None)
    if isinstance(x, str):
        if _OBJ_REPR_RE.match(x):  # repr opaque
            return Any
        return str
    if isinstance(x, (bytes, bytearray, memoryview)):
        return bytes
    if isinstance(x, (int, float, complex, bool)):
        return _typeof(x)
    if isinstance(x, (list, tuple)):
        return _merge_seq(x)
    if isinstance(x, dict):
        return _merge_mapping(x)
    if isinstance(x, (set, frozenset)):
        inner = _merge_seq(list(x))
        T = (
            get_args(inner)[0]
            if get_origin(inner) in (list, TSequence) and get_args(inner)
            else Any
        )
        return set[T] if isinstance(x, set) else frozenset[T]
    # Tout objet non trivial → Any (garantit robustesse)
    return Any


def _maybe_optional(tp: Any) -> Any:
    if get_origin(tp) is Union:
        args = tuple(get_args(tp))
        if type(None) in args:
            rest = tuple(t for t in args if t is not type(None))
            if len(rest) == 1:
                return Optional[rest[0]]
            return Optional[Union[rest]]
    return tp


# ------------------ Coalescing d'unions paramétrées ------------------


def _same_origin_mappings(args: list[Any]) -> bool:
    from collections.abc import Mapping as _M

    def is_mapping(t):
        return get_origin(t) in (_M, dict)

    return all(is_mapping(a) for a in args) and len(args) > 0


def _same_origin_sequences(args: list[Any]) -> bool:
    from collections.abc import Sequence as _S

    def is_seq(t):
        o = get_origin(t)
        return o in (_S, list, tuple)

    return all(is_seq(a) for a in args) and len(args) > 0


def _coalesce_parametrized_union(tp: Any) -> Any:
    """Union de mappings/sequences → un seul mapping/sequence avec Union des paramètres."""
    if get_origin(tp) is not Union:
        return tp
    args = list(get_args(tp))

    # D'abord, applique récursivement à chaque membre
    args = [_coalesce_parametrized_union(a) for a in args]

    # Si tous des Mapping[K,V] ou dict[K,V]
    if _same_origin_mappings(args):
        K = V = None
        for a in args:
            k, v = get_args(a)
            K = k if K is None else _merge(K, k)
            V = v if V is None else _merge(V, v)
        return TMapping[K or Any, V or Any]

    # Si tous des séquences (list/tuple/Sequence) → Sequence[Union[...]]
    if _same_origin_sequences(args):
        T = None
        for a in args:
            # list[T] / tuple[T] / Sequence[T] -> extraire T
            t_args = get_args(a)
            inner = t_args[0] if t_args else Any
            T = inner if T is None else _merge(T, inner)
        return TSequence[T or Any]

    # Sinon, reconstruit une Union aplatie/dédupliquée/plafonnée
    flat = []
    for a in args:
        flat.extend(_flatten_union_types(a))
    uniq = _dedup_types(flat)
    if len(uniq) > MAX_UNION:
        return Any
    return Union[tuple(uniq)]


# ------------------ Résumé typé par fonction ------------------


@dataclass
class FunctionTypeInfo:
    signature: inspect.Signature
    param_types: dict[str, Any]
    return_type: Any
    samples: int


def _surrogate_signature_from_samples(samples: list[dict]) -> inspect.Signature:
    """Signature de secours quand la vraie fonction n'est pas importable."""
    max_pos = 0
    kw_names: set[str] = set()
    for s in samples:
        max_pos = max(max_pos, len(s.get("args", ()) or ()))
        kw_names.update((s.get("kwargs") or {}).keys())
    params: list[inspect.Parameter] = []
    for i in range(max_pos):
        params.append(
            inspect.Parameter(f"arg{i}", kind=inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
    for name in sorted(kw_names):
        params.append(
            inspect.Parameter(name, kind=inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
    return inspect.Signature(params)


def summarize_function_types(func_fqname: str, samples: list[dict]) -> FunctionTypeInfo:
    """Essaye d'importer la fonction ; sinon, utilise une signature de secours."""
    try:
        mod_name, fn_name = func_fqname.rsplit(".", 1)
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name)
        sig = inspect.signature(fn)
        bind = lambda args, kwargs: sig.bind_partial(*args, **(kwargs or {}))
    except Exception:
        sig = _surrogate_signature_from_samples(samples)
        bind = lambda args, kwargs: sig.bind_partial(*args, **(kwargs or {}))

    acc: dict[str, Any] = {}
    ret: Any = None

    for s in samples:
        args = list(s.get("args", ()))
        kwargs = dict(s.get("kwargs", {}) or {})
        ba = bind(args, kwargs)
        for name, val in ba.arguments.items():
            t = infer_type(val)
            acc[name] = t if name not in acc else _merge(acc[name], t)
        r = infer_type(s.get("result"))
        ret = r if ret is None else _merge(ret, r)

    # Normalisation finale (optionalisation + coalescing)
    acc = {k: _coalesce_parametrized_union(_maybe_optional(t)) for k, t in acc.items()}
    ret = _coalesce_parametrized_union(_maybe_optional(ret)) if ret is not None else Any
    return FunctionTypeInfo(
        signature=sig, param_types=acc, return_type=ret, samples=len(samples)
    )


# ------------------ Rendu .pyi ------------------


def _qname(tp: Any) -> str:
    mod = getattr(tp, "__module__", "")
    name = getattr(tp, "__qualname__", getattr(tp, "__name__", str(tp)))
    q = f"{mod}.{name}" if mod and mod != "builtins" else name
    return NAME_ALIASES.get(q, q)


def _format_type(tp: Any) -> str:
    # Coalesce en amont pour raccourcir la forme affichée
    tp = _coalesce_parametrized_union(tp)

    if get_origin(tp) is Union:
        args = list(get_args(tp))
        if type(None) in args:
            args.remove(type(None))
            inner = " | ".join(_format_type(a) for a in args) or "Any"
            return f"Optional[{inner}]"
        return " | ".join(_format_type(a) for a in args)

    origin = get_origin(tp)
    if origin is not None:
        args = ", ".join(_format_type(a) for a in get_args(tp))
        name = _qname(origin)
        if name.startswith("typing."):
            name = name.split(".", 1)[1]
        return f"{name}[{args}]"

    if tp in (Any,):
        return "Any"
    if isinstance(tp, type) and tp.__module__ == "builtins":
        return tp.__name__
    mod = getattr(tp, "__module__", "")
    nm = getattr(tp, "__qualname__", getattr(tp, "__name__", str(tp)))
    if mod == "typing":
        return nm
    qn = _qname(tp)
    return f'"{qn}"'


def render_stub_for_function(func_name: str, info: FunctionTypeInfo) -> str:
    sig = info.signature
    parts = []
    for p in sig.parameters.values():
        ann = info.param_types.get(p.name, Any)
        a_txt = _format_type(ann)
        prefix = (
            "*"
            if p.kind is p.VAR_POSITIONAL
            else "**"
            if p.kind is p.VAR_KEYWORD
            else ""
        )
        default = "" if p.default is p.empty else " = ..."
        parts.append(f"{prefix}{p.name}: {a_txt}{default}")
    ret_txt = _format_type(info.return_type or Any)
    return f"def {func_name}({', '.join(parts)}) -> {ret_txt}: ..."


def render_stub_module(module: str, funcs: dict[str, FunctionTypeInfo]) -> str:
    lines = [
        "# Auto-generated by pytead types — DO NOT EDIT.",
        "from typing import *",
        "",
    ]
    for fn in sorted(funcs):
        lines.append(render_stub_for_function(fn, funcs[fn]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def group_by_module(
    fn_infos: dict[str, FunctionTypeInfo]
) -> dict[str, dict[str, FunctionTypeInfo]]:
    out: dict[str, dict[str, FunctionTypeInfo]] = defaultdict(dict)
    for fqname, info in fn_infos.items():
        mod, fn = fqname.rsplit(".", 1)
        out[mod][fn] = info
    return dict(out)
