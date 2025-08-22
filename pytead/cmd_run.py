import argparse
import runpy
import sys
from pathlib import Path
from typing import List, Tuple

from .tracing import trace
from .storage import get_storage
from .logconf import configure_logger
from ._cli_utils import split_targets_and_cmd, fallback_targets_from_cfg
from .config import (
    apply_config_from_default_file,
    get_effective_config,
    LAST_CONFIG_PATH,
)
from ._targets import resolve_target
import inspect


def _handle(args: argparse.Namespace) -> None:
    """Instrument one or more functions/methods and run the target script."""
    logger = configure_logger(name="pytead.cli.run")

    # 0) Raw args as parsed by argparse (before config injection)
    logger.info("RUN: raw args (pre-config): %s", {k: getattr(args, k) for k in vars(args)})

    # 0bis) Prefer searching config from the script's directory if provided
    start_hint = None
    try:
        for tok in (getattr(args, "cmd", []) or []):
            if tok.endswith(".py"):
                start_hint = Path(tok).resolve().parent
                break
    except Exception:
        start_hint = None

    # 1) Fill from default_config (does NOT override explicit CLI flags)
    apply_config_from_default_file("run", args, start=start_hint)

    # 2) Effective args after config
    logger.info("RUN: effective args (post-config): %s", {k: getattr(args, k) for k in vars(args)})

    # 3) Validate required options (no in-code defaults here)
    missing = [k for k in ("limit", "storage_dir", "format") if not hasattr(args, k)]
    if missing:
        logger.error(
            "Missing required options for 'run': %s. "
            "Set them in [defaults]/[run] of .pytead/config.(toml|yaml) or pass flags.",
            ", ".join(missing),
        )
        sys.exit(1)

    # Snapshot effective config for potential fallback of targets
    effective_cfg = get_effective_config("run", start=start_hint)
    logger.info("RUN: effective config snapshot: %s", effective_cfg or "{}")

    # 4) Split positionals into targets and script command
    targets, cmd = split_targets_and_cmd(
        getattr(args, "targets", []) or [],
        getattr(args, "cmd", []),
    )
    logger.info("RUN: split targets=%s cmd=%s", targets, cmd)

    # Fallback to config-provided targets if empty
    targets = fallback_targets_from_cfg(targets, effective_cfg, logger, "RUN")

    if not targets:
        logger.error(
            "No target provided. Expect at least one 'module.function' or 'module.Class.method'. "
            "Config file used: %s",
            LAST_CONFIG_PATH,
        )
        sys.exit(1)
    if not cmd:
        logger.error("No script specified after '--'")
        sys.exit(1)

    # 5) Prepare import path and storage backend
    sys.path.insert(0, str(Path.cwd()))
    storage = get_storage(args.format)

    # 6) Resolve and instrument (descriptor-aware)
    resolved: List[Tuple[object, str, str]] = []
    errors: List[str] = []

    for t in targets:
        try:
            rt = resolve_target(t)  # supports module.func and module.Class.method
            resolved.append((rt.owner, t, rt.kind))
        except Exception as exc:
            errors.append(f"Cannot resolve target '{t}': {exc}")

    if errors:
        for m in errors:
            logger.error(m)
        sys.exit(1)

    seen = set()
    for owner, fq, kind in resolved:
        key = fq  # deduplicate on fully qualified target name
        if key in seen:
            continue
        seen.add(key)

        name = fq.split(".")[-1]
        raw = inspect.getattr_static(owner, name)

        if isinstance(raw, staticmethod):
            fn = raw.__func__
            wrapped = trace(limit=args.limit, storage_dir=args.storage_dir, storage=storage)(fn)
            setattr(owner, name, staticmethod(wrapped))
        elif isinstance(raw, classmethod):
            fn = raw.__func__
            wrapped = trace(limit=args.limit, storage_dir=args.storage_dir, storage=storage)(fn)
            setattr(owner, name, classmethod(wrapped))
        else:
            # Module-level function, or function stored on a class (instance method).
            fn = getattr(owner, name)
            wrapped = trace(limit=args.limit, storage_dir=args.storage_dir, storage=storage)(fn)
            setattr(owner, name, wrapped)

    logger.info(
        "Instrumentation applied to %d target(s): %s",
        len(seen),
        ", ".join(sorted(seen)),
    )

    # 7) Execute the target script
    script = cmd[0]
    if not script.endswith(".py"):
        logger.error("Unsupported script '%s': only .py files are allowed", script)
        sys.exit(1)

    sys.argv = cmd
    try:
        runpy.run_path(script, run_name="__main__")
    except Exception as exc:
        logger.error("Error during script execution: %s", exc)
        sys.exit(1)



def add_run_subparser(subparsers) -> None:
    p_run = subparsers.add_parser(
        "run",
        help="instrument one or more functions/methods and execute a Python script (use -- to separate targets from the script)",
    )
    # No hard-coded defaults: config file fills missing values
    p_run.add_argument(
        "-l",
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="max number of calls to record per target",
    )
    p_run.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory to store trace files",
    )
    p_run.add_argument(
        "--format",
        choices=["pickle", "json", "repr"],
        default=argparse.SUPPRESS,
        help="trace storage format",
    )
    p_run.add_argument(
        "targets",
        nargs="*",
        default=argparse.SUPPRESS,
        metavar="target",
        help=(
            "one or more targets to trace: 'module.function' or 'module.Class.method' "
            "(may be provided via config [run].targets)"
        ),
    )
    p_run.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- then the Python script to run (with arguments)",
    )
    p_run.set_defaults(handler=_handle)

