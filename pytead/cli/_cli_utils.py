
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Optional, Callable

from .config_cli import diagnostics_for_storage_dir, resolve_under_project_root
from .config_cli import load_layered_config, apply_effective_to_args






__all__ = [
    "unique_count",
    "fallback_targets_from_cfg",
    "emptyish",
    "require_script_py_or_exit",
    "resolve_output_paths",
    "ensure_storage_dir_or_exit",
    "resolve_additional_sys_path",
    "resolve_under",
]





    
def unique_count(entries_by_func):
    """
    Count unique cases.
    - graph-json: uniqueness by JSON value of (args_graph, kwargs_graph, result_graph)
    - pickle (legacy state-based): reuse TraceCase hashing
    """
    import json
    total = 0
    for entries in entries_by_func.values():
        if entries and ("args_graph" not in entries[0]):
            from .._cases import unique_cases  # lazy import
            total += len(unique_cases(entries))
            continue
        def _norm(x):  # graph-json
            return json.dumps(x, sort_keys=True, ensure_ascii=False)
        seen = {
            (_norm(e.get("args_graph")), _norm(e.get("kwargs_graph")), _norm(e.get("result_graph")))
            for e in entries
        }
        total += len(seen) if seen else len(entries)
    return total


def fallback_targets_from_cfg(
    targets: List[str],
    effective_cfg: Dict[str, Any] | None,
    logger: Any,
    label: str,
) -> List[str]:
    """
    If `targets` is empty, try to fill it from the effective config (e.g., [run].targets).

    Parameters
    ----------
    targets
        Current list of CLI targets (possibly empty after splitting).
    effective_cfg
        The merged config for the relevant section (e.g., "run" or "tead").
    logger
        Logger-like object (must support .info()) used for diagnostics.
    label
        String used to tag log messages (e.g., "RUN" or "TEAD").

    Returns
    -------
    List[str]
        Either the original non-empty `targets`, or targets taken from config.
    """
    if targets:
        return targets
    cfg_targets = (effective_cfg or {}).get("targets")
    if cfg_targets:
        logger.info(
            "%s: no CLI targets after split; falling back to config targets: %s",
            label,
            cfg_targets,
        )
        return list(cfg_targets)
    return targets
    
    

def emptyish(x) -> bool:
    """None, '' , [], {} → True (utile pour args de CLI / config)."""
    return x is None or (isinstance(x, (str, list, dict)) and len(x) == 0)




def require_script_py_or_exit(cmd: List[str], logger: Any) -> Path:
    """
    Vérifie que cmd[0] est bien un fichier .py. Loggue et exit(1) sinon.
    Retourne le Path résolu du script.
    """
    if not cmd:
        logger.error("No script specified after '--'")
        import sys as _sys
        _sys.exit(1)
    script = cmd[0]
    if not isinstance(script, str) or not script.endswith(".py"):
        logger.error("Unsupported script '%s': only .py files are allowed", script)
        import sys as _sys
        _sys.exit(1)
    try:
        return Path(script).resolve()
    except Exception:
        return Path(script)


def resolve_output_paths(
    output: Optional[Path],
    output_dir: Optional[Path],
    default_single_file: Path,
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Politique commune (-o/-d): si ni -o ni -d, on choisit un fichier unique.
    Retourne (output, output_dir) finalisés (sans création).
    """
    if emptyish(output) and emptyish(output_dir):
        return default_single_file, None
    return output, output_dir


def ensure_storage_dir_or_exit(ctx, section: str, storage_dir_value: Optional[Path | str], logger: Any) -> Path:
    """
    Valide l'option storage_dir (présence + existence répertoire). Loggue diagnostics
    enrichis (y compris rapport de config) et exit(1) en cas d'erreur. Retourne le Path.
    """
    import sys as _sys
    if emptyish(storage_dir_value):
        logger.error(
            "%s: missing 'storage_dir'. Ensure it exists in [defaults] or [%s], "
            "or pass --storage-dir. Config used: %s",
            section.upper(), section,
            str(getattr(ctx, "source_path", None)) if getattr(ctx, "source_path", None) else "<none>",
        )
        try:
            diag = diagnostics_for_storage_dir(ctx, section, storage_dir_value)
            logger.error("\n%s", diag)
        except Exception:
            pass
        _sys.exit(1)

    p = resolve_under_project_root(ctx, storage_dir_value)
    if not p.exists() or not p.is_dir():
        logger.error("Storage (calls) directory '%s' does not exist or is not a directory", p)
        try:
            diag = diagnostics_for_storage_dir(ctx, section, p)
            logger.error("\n%s", diag)
        except Exception:
            pass
        _sys.exit(1)

    return p


def resolve_additional_sys_path(project_root: Path, raw_paths: Iterable[str]) -> List[str]:
    """
    Normalise des chemins supplémentaires d'import: résout relatifs par rapport à project_root,
    déduplique, retourne des str absolus.
    """
    out: List[str] = []
    seen: set[str] = set()
    for p in raw_paths or []:
        pp = Path(p)
        ap = pp if pp.is_absolute() else (project_root / pp)
        try:
            s = str(ap.resolve())
        except Exception:
            s = str(ap)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def resolve_under(root: Path, p: Optional[Path]) -> Optional[Path]:
    """Si p est relatif, retour (root / p), sinon p; passthrough None."""
    if p is None:
        return None
    return p if p.is_absolute() else (root / p)



def resolve_storage_dir_for_write(ctx, storage_dir_value: Optional[Path | str], logger) -> Path:
    """Resolve under project_root and mkdir for write-mode commands (run/tead)."""
    p = resolve_under_project_root(ctx, storage_dir_value)
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error("Cannot create storage_dir '%s': %s", p, exc)
        _sys.exit(1)
    return p
    

def require_output_dir_or_exit(ctx, out_dir_value: Optional[Path | str], logger, section: str) -> Path:
    """Uniformize 'output_dir is required' policy and resolution."""
    if out_dir_value is None:
        logger.error("`pytead %s` requires --output-dir or [%s].output_dir in config.", section, section)
        _sys.exit(1)
    return resolve_under_project_root(ctx, out_dir_value)

def extract_script_and_argv(remainder: list[str], logger) -> Tuple[Path, list[str]]:
    """Strip leading '--', validate .py, and return (script_path, argv)."""
    cmd = list(remainder or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    from ._cli_utils import require_script_py_or_exit  # self-import ok
    script_path = require_script_py_or_exit(cmd, logger)
    return script_path, cmd  # cmd includes script + its args; run_script sets sys.argv

def require_targets_or_exit(targets: list[str], ctx, logger, missing_hint_sections=("tead","run")) -> list[str]:
    if targets:
        return targets
    logger.error(
        "No target provided. Expect at least one 'module.function' or 'module.Class.method'. "
        "Config file used: %s ; checked sections: %s",
        str(getattr(ctx, "source_path", None) or "<none>"),
        ", ".join(f"[{s}].targets" for s in missing_hint_sections),
    )
    _sys.exit(1)

def normalize_additional_sys_path(project_root: Path, raw_paths: Iterable[str] | None) -> list[str]:
    """
    Compute import roots for CLI commands.
    Policy:
      - Always include project_root first.
      - Then resolve each additional path (relative under project_root or absolute).
      - Return absolute strings, deduped with order preserved.
    """
    pr = str(project_root.resolve())
    roots: list[str] = [pr]

    seen = {pr}
    for raw in raw_paths or []:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = (project_root / p).resolve()
        else:
            p = p.resolve()
        s = str(p)
        if s not in seen:
            seen.add(s)
            roots.append(s)

    return roots
    
def load_ctx_and_fill(section: str, args, start_getter: Callable[[object], Optional[Path]]):
    start = start_getter(args)
    ctx = load_layered_config(start=start)
    apply_effective_to_args(section, ctx, args)
    
    # Fallbacks: when section == "tead", inherit missing keys from "run"
    if section == "tead":
        try:
            from ._common import eff
            eff_tead = eff(ctx, "tead")
            eff_run  = eff(ctx, "run")
            for key in ("additional_sys_path", "targets"):
                if getattr(args, key, None) in (None, [], ()):
                    val = eff_tead.get(key)
                    if not val:
                        val = eff_run.get(key)
                    if val:
                        setattr(args, key, val)
        except Exception:
            # Soft-fail: if eff or ctx layout changes, don't break the CLI
            pass
    return ctx
