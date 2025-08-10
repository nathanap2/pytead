import argparse
import fnmatch
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .storage import storages_from_names
from .logconf import configure_logger
from .config import apply_config_from_default_file


def _parse_before(s: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO8601-like timestamp and return a UTC datetime, or None.

    Accepted inputs:
      - "YYYY-MM-DD"  -> interpreted as the *end of that UTC day*
                         (23:59:59.999999Z), so it deletes everything strictly
                         before that end-of-day timestamp.
      - Full ISO, with optional trailing "Z" (UTC).

    Returned value is timezone-aware (UTC).
    """
    if not s:
        return None
    txt = s.strip()

    # Date-only -> end of day in UTC to include the whole day.
    if len(txt) == 10 and txt[4] == "-" and txt[7] == "-":
        dt = datetime.fromisoformat(txt).replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
        )
        return dt

    # Accept trailing 'Z' as UTC designator.
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"

    dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _matches_func(name: str, exact: List[str], globs: List[str]) -> bool:
    """Return True if 'name' matches either an exact target or any glob pattern."""
    if not exact and not globs:
        return True
    if name in exact:
        return True
    for pat in globs:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _plan_deletions(
    calls_dir: Path,
    formats: Optional[List[str]],
    functions: List[str],
    glob_patterns: List[str],
    before_iso: Optional[str],
    logger: logging.Logger,
) -> Tuple[List[Path], int]:
    """
    Compute the deletion plan by scanning 'calls_dir' for known storage formats.

    Returns:
        (paths_to_delete, total_size_bytes)
    """
    storages = storages_from_names(formats)
    ext_to_storage = {st.extension: st for st in storages}

    # Collect candidate files by extension.
    candidates: List[Path] = []
    for ext in ext_to_storage.keys():
        candidates.extend(sorted(calls_dir.glob(f"*{ext}")))

    before_dt = _parse_before(before_iso)
    to_delete: List[Path] = []
    total_bytes = 0

    for p in candidates:
        st = ext_to_storage.get(p.suffix)
        if st is None:
            continue
        try:
            entry = st.load(p)
        except Exception as exc:
            logger.warning("Skipping corrupt/unreadable trace %s: %s", p, exc)
            continue

        func = entry.get("func")
        if not isinstance(func, str):
            logger.warning("Skipping trace without valid 'func': %s", p)
            continue
        if not _matches_func(func, functions or [], glob_patterns or []):
            continue

        if before_dt is not None:
            ts = entry.get("timestamp")
            if not isinstance(ts, str):
                logger.debug("Skipping %s: missing/invalid timestamp", p)
                continue
            ts_txt = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            try:
                ts_dt = datetime.fromisoformat(ts_txt)
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                ts_dt = ts_dt.astimezone(timezone.utc)
            except Exception:
                logger.debug("Skipping %s: unparsable timestamp %r", p, ts)
                continue
            if not (ts_dt < before_dt):
                continue

        try:
            sz = p.stat().st_size
        except Exception:
            sz = 0
        total_bytes += sz
        to_delete.append(p)

    # Deterministic order for display and reproducibility
    to_delete.sort(key=lambda q: (q.suffix, q.name))
    return to_delete, total_bytes


def run(args) -> None:
    logger = configure_logger(name="pytead.clean")

    # Fill args from default_config.toml: [defaults] → [clean]
    apply_config_from_default_file("clean", args)

    calls_dir: Path = Path(getattr(args, "calls_dir", Path("call_logs")))
    if not calls_dir.exists() or not calls_dir.is_dir():
        logger.error(
            "Calls directory '%s' does not exist or is not a directory", calls_dir
        )
        sys.exit(1)

    to_delete, total_bytes = _plan_deletions(
        calls_dir=calls_dir,
        formats=getattr(args, "formats", None),
        functions=getattr(args, "functions", []) or [],
        glob_patterns=getattr(args, "glob", []) or [],
        before_iso=getattr(args, "before", None),
        logger=logger,
    )

    if not to_delete:
        logger.info("Nothing to delete (no traces matched the given filters).")
        return

    if getattr(args, "dry_run", False):
        logger.info(
            "Dry-run: %d file(s) would be removed (~%.1f KB). Use -y to confirm for real.",
            len(to_delete),
            total_bytes / 1024.0,
        )
        # Keep listing to stdout for easy piping/inspection
        for p in to_delete:
            print(p)
        return

    if not getattr(args, "yes", False):
        # Do not prompt on non-interactive stdin; require -y in pipelines/CI.
        if not sys.stdin.isatty():
            logger.error("Refusing to prompt on non-interactive stdin. Use -y.")
            sys.exit(1)
        print(
            f"About to delete {len(to_delete)} file(s) (~{total_bytes/1024.0:.1f} KB). "
            f"Continue? [y/N] ",
            end="",
        )
        try:
            choice = input().strip().lower()
        except EOFError:
            choice = "n"
        if choice not in ("y", "yes"):
            print("Aborted.")
            return

    removed = 0
    for p in to_delete:
        try:
            p.unlink()
            removed += 1
        except Exception as exc:
            logger.warning("Failed to remove %s: %s", p, exc)

    logger.info(
        "Removed %d file(s) (~%.1f KB) from '%s'.",
        removed,
        total_bytes / 1024.0,
        calls_dir,
    )


def add_subparser(subparsers) -> None:
    """
    Register the `clean` subcommand.

    Defaults are *suppressed* so they can be filled from default_config.toml
    (section [clean] merged over [defaults]) by apply_config_from_default_file().
    """
    p_clean = subparsers.add_parser(
        "clean",
        help="delete recorded trace files with optional filters (function, glob, date, format)",
    )
    p_clean.add_argument(
        "-c",
        "--calls-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing trace files (default from config, else call_logs/)",
    )
    p_clean.add_argument(
        "--formats",
        choices=["pickle", "json", "repr"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict deletion to these formats (default: all detected)",
    )
    p_clean.add_argument(
        "--func",
        dest="functions",
        action="append",
        default=argparse.SUPPRESS,
        help="limit deletion to this fully-qualified function (repeatable)",
    )
    p_clean.add_argument(
        "--glob",
        dest="glob",
        action="append",
        default=argparse.SUPPRESS,
        help="fnmatch-style pattern over fully-qualified function (repeatable), e.g. 'mymodule.*'",
    )
    p_clean.add_argument(
        "--before",
        type=str,
        default=argparse.SUPPRESS,
        help="delete traces strictly older than this date/time (YYYY-MM-DD or ISO8601, accepts trailing 'Z')",
    )
    p_clean.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show which files would be deleted without removing them",
    )
    p_clean.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not prompt for confirmation",
    )
    # Important: use 'handler' to avoid collision with '--func'
    p_clean.set_defaults(handler=run)
