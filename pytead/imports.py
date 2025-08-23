# pytead/imports.py
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Union, Optional
import os
import sys

Pathish = Union[str, os.PathLike[str], Path]


def _auto_detect_project_root(start: Optional[Path]) -> Path:
    """
    Best-effort project root detection:
      - From `start` (script dir) upward: directory containing '.pytead' or 'pyproject.toml'
      - Fallback: CWD
    """
    base = (start or Path.cwd()).resolve()
    for p in [base] + list(base.parents):
        if (p / ".pytead").exists() or (p / "pyproject.toml").exists():
            return p
    return Path.cwd().resolve()


def _to_abs_dir(p: Path) -> Path | None:
    try:
        ap = p.resolve()
    except Exception:
        ap = p
    return ap if ap.is_dir() else None


def compute_import_roots(
    script_path: Pathish | None,
    additional: Iterable[Pathish] | None = None,
    project_root: Optional[Path] = None,  # NEW
) -> List[str]:
    """
    Build ordered import roots to prepend to sys.path:
      1) script directory (if provided)
      2) project root (explicit or auto-detected)
      3) additional paths (relative to project root if not absolute)
    Return absolute, de-duplicated strings.
    """
    roots: list[Path] = []
    proj_root = project_root or _auto_detect_project_root(
        Path(script_path).parent if (script_path and str(script_path).endswith(".py")) else None
    )

    # 1) script dir
    if script_path:
        sp = Path(script_path)
        if sp.suffix == ".py":
            sd = _to_abs_dir(sp.parent)
            if sd is not None:
                roots.append(sd)

    # 2) project root
    pr = _to_abs_dir(proj_root)
    if pr is not None:
        roots.append(pr)

    # 3) additionals
    for raw in (additional or []):
        p = Path(raw)
        abs_p = p if p.is_absolute() else (proj_root / p)
        ap = _to_abs_dir(abs_p)
        if ap is not None:
            roots.append(ap)

    # de-dup while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for rr in roots:
        s = rr.as_posix()
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def prepend_sys_path(roots: Iterable[Pathish]) -> None:
    """
    Prepend sys.path with the given roots (strings or Paths),
    preserving input order and avoiding duplicates.
    """
    normed: list[str] = []
    seen: set[str] = set()
    for r in roots:
        p = Path(r)
        try:
            s = p.resolve().as_posix()
        except Exception:
            s = p.as_posix()
        if s not in seen:
            seen.add(s)
            normed.append(s)

    for s in reversed(normed):
        if s not in sys.path:
            sys.path.insert(0, s)

