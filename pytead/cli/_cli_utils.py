
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Optional

from .config_cli import diagnostics_for_storage_dir

from .._cases import unique_cases


__all__ = [
    "split_targets_and_cmd",
    "unique_count",
    "fallback_targets_from_cfg",
    "emptyish",
    "first_py_token",
    "require_script_py_or_exit",
    "resolve_output_paths",
    "ensure_storage_dir_or_exit",
    "resolve_additional_sys_path",
    "resolve_under"
]


def split_targets_and_cmd(
    targets: List[str] | None,
    cmd: List[str] | None,
) -> Tuple[List[str], List[str]]:
    """
    Robustly split positional arguments into function targets and the script command.

    Rules:
    - If '--' accidentally ended up in `targets`, split there and move the tail to `cmd`.
    - Strip a leading '--' token from `cmd` if present.
    - If a '*.py' token was mistakenly placed among `targets`, move that token and
      everything after it to `cmd`.

    This function never mutates the input lists; it returns new lists.
    """
    t = list(targets or [])
    c = list(cmd or [])

    # Case 1: '--' slipped into targets → split and move the tail to cmd
    if "--" in t:
        sep = t.index("--")
        c = t[sep + 1 :] + c
        t = t[:sep]

    # Case 2: defensive — argparse.REMAINDER may give a leading '--' in cmd
    if c and c[0] == "--":
        c = c[1:]

    # Case 3: user forgot '--' and put a script in targets → move '*.py' and the rest
    for i, tok in enumerate(t):
        if tok.endswith(".py"):
            c = t[i:] + c
            t = t[:i]
            break

    return t, c


def unique_count(entries_by_func: Dict[str, List[Dict[str, Any]]]) -> int:
    """
    Return the total number of unique test cases across all functions.
    """
    return sum(len(unique_cases(entries)) for entries in entries_by_func.values())



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


def first_py_token(tokens) -> Optional[Path]:
    """Retourne le premier Path *.py trouvé dans une liste de tokens argparse."""
    for tok in tokens or []:
        if isinstance(tok, str) and tok.endswith(".py"):
            try:
                return Path(tok).resolve()
            except Exception:
                return Path(tok)
    return None


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

    p = Path(storage_dir_value)
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

