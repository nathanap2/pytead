from typing import Any, Dict, List, Tuple

from .._cases import unique_cases


__all__ = [
    "split_targets_and_cmd",
    "unique_count",
    "fallback_targets_from_cfg",
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

    Uniqueness is determined by `unique_cases(...)`, which normalizes
    (args, kwargs, expected) and deduplicates identical triples.
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
