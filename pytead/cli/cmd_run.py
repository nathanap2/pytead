# pytead/cli/cmd_run.py
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from typing import List

from ..logconf import configure_logger
from ..storage import get_storage
from ..targets import instrument_targets
from ..imports import prepend_sys_path
from ._cli_utils import split_targets_and_cmd, fallback_targets_from_cfg
from .config_cli import load_layered_config, apply_effective_to_args, effective_section


def _handle(args: argparse.Namespace) -> None:
    """Instrument one or more functions/methods and run the target script."""
    logger = configure_logger(name="pytead.cli.run")

    # Prefer discovering config from the script's directory if provided
    start_hint = None
    try:
        for tok in (getattr(args, "cmd", []) or []):
            if isinstance(tok, str) and tok.endswith(".py"):
                start_hint = Path(tok).resolve().parent
                break
    except Exception:
        start_hint = None

    # Load layered config (packaged < user < project) and fill missing/empty args
    ctx = load_layered_config(start=start_hint)
    apply_effective_to_args("run", ctx, args)

    # Validate required options (no hard-coded defaults here)
    missing = [k for k in ("limit", "storage_dir", "format") if not hasattr(args, k)]
    if missing:
        logger.error(
            "Missing required options for 'run': %s. "
            "Set them in [defaults]/[run] of .pytead/config.(toml|yaml) or pass flags.",
            ", ".join(missing),
        )
        sys.exit(1)

    eff_run = effective_section(ctx, "run")

    # Split positionals into targets and script command
    targets, cmd = split_targets_and_cmd(
        getattr(args, "targets", []) or [],
        getattr(args, "cmd", []),
    )

    # Fallback to config-provided targets if empty
    targets = fallback_targets_from_cfg(targets, eff_run, logger, "RUN")

    if not targets:
        logger.error(
            "No target provided. Expect at least one 'module.function' or 'module.Class.method'. "
            "Config file used: %s",
            str(ctx.source_path) if ctx.source_path else "<none>",
        )
        sys.exit(1)
    if not cmd:
        logger.error("No script specified after '--'")
        sys.exit(1)

    # Prepare import environment (script dir, project root, additional_sys_path)
    script_path = None
    if cmd and isinstance(cmd[0], str) and cmd[0].endswith(".py"):
        script_path = Path(cmd[0]).resolve()

    roots: List[str] = []
    if script_path:
        roots.append(str(script_path.parent))
    roots.append(str(ctx.project_root))
    extra: List[str] = []
    for p in (getattr(args, "additional_sys_path", []) or []):
        pp = Path(p)
        # Resolve relative extra paths against the project root discovered by config
        ap = pp if pp.is_absolute() else (ctx.project_root / pp)
        try:
            extra.append(str(ap.resolve()))
        except Exception:
            extra.append(str(ap))
    roots.extend(extra)
    prepend_sys_path(roots)

    # Prepare storage backend
    storage = get_storage(getattr(args, "format", None))

    # Resolve & instrument
    try:
        seen = instrument_targets(
            targets,
            limit=getattr(args, "limit"),
            storage_dir=getattr(args, "storage_dir"),
            storage=storage,
        )
    except Exception as exc:
        for line in str(exc).splitlines():
            logger.error(line)
        sys.exit(1)

    logger.info(
        "Instrumentation applied to %d target(s): %s",
        len(seen),
        ", ".join(sorted(seen)),
    )

    # Execute the target script
    script = cmd[0]
    if not isinstance(script, str) or not script.endswith(".py"):
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
        help=(
            "instrument one or more functions/methods and execute a Python script "
            "(use -- to separate targets from the script)"
        ),
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
            "one or more targets: 'module.function' or 'module.Class.method' "
            "(may be provided via config [run].targets)"
        ),
    )
    p_run.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- then the Python script to run (with arguments)",
    )
    p_run.set_defaults(handler=_handle)

