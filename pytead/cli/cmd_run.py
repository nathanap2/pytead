# pytead/cli/cmd_run.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from ..errors import PyteadError
from ..logconf import configure_logger
from ._cli_utils import split_targets_and_cmd, fallback_targets_from_cfg
from .config_cli import load_layered_config, apply_effective_to_args, effective_section
from . import service_cli as svc  # couche services dans le même paquet CLI


def _first_py(tokens: Iterable[str] | None) -> Optional[Path]:
    for t in tokens or []:
        if isinstance(t, str) and t.endswith(".py"):
            try:
                return Path(t).resolve()
            except Exception:
                return Path(t)
    return None


def _require_fields(args: argparse.Namespace, names: list[str], logger) -> None:
    missing = [n for n in names if not hasattr(args, n)]
    if missing:
        logger.error(
            "Missing required options for 'run': %s. Provide them via CLI or config "
            "([defaults]/[run]).",
            ", ".join(missing),
        )
        sys.exit(1)


def _handle(args: argparse.Namespace) -> None:
    """
    Thin handler: merges layered config into args, validates, then delegates to service layer.
    """
    log = configure_logger(name="pytead.cli.run")

    # Prefer discovering config from the script directory if provided
    start_hint = _first_py(getattr(args, "cmd", None))
    ctx = load_layered_config(start=start_hint)
    apply_effective_to_args("run", ctx, args)

    # Required fields can come from config
    _require_fields(args, ["limit", "storage_dir", "format"], log)

    # Split positionals into targets and the script command
    targets, cmd = split_targets_and_cmd(
        getattr(args, "targets", []) or [],
        getattr(args, "cmd", []) or [],
    )

    # Fallback to config-provided targets if none on the CLI
    eff_run = effective_section(ctx, "run")
    targets = fallback_targets_from_cfg(targets, eff_run, log, "RUN")

    if not targets:
        log.error(
            "No target provided. Expect at least one 'module.function' or 'module.Class.method'. "
            "Config file used: %s",
            str(ctx.source_path) if ctx.source_path else "<none>",
        )
        sys.exit(1)
    if not cmd:
        log.error("No script specified after '--'")
        sys.exit(1)

    # Validate script path
    script = cmd[0]
    if not isinstance(script, str) or not script.endswith(".py"):
        log.error("Unsupported script '%s': only .py files are allowed", script)
        sys.exit(1)
    script_path = _first_py([script])
    assert script_path is not None

    # Additional import roots:
    # Keep them as given (possibly relative). compute_import_roots will anchor them on project root.
    raw_extra = getattr(args, "additional_sys_path", None) or []
    add_paths: List[Path] = [Path(p) for p in raw_extra]

    # Delegate to service layer: prepare imports, instrument, run
    try:
        instr, outcome, roots = svc.instrument_and_run(
            targets=targets,
            limit=getattr(args, "limit"),
            storage_dir=Path(getattr(args, "storage_dir")),
            storage=getattr(args, "format"),
            script_file=script_path,
            argv=cmd,
            additional_sys_path=add_paths,
            logger=log,
        )
    except PyteadError:
        raise
    except Exception as exc:
        log.error("Run failed: %s", exc)
        sys.exit(1)

    # Summaries
    log.info(
        "Instrumentation applied to %d target(s): %s",
        len(instr.seen),
        ", ".join(sorted(instr.seen)),
    )
    if outcome.status.name == "OK":
        log.info("Script run completed successfully.")
    elif outcome.status.name == "SYSTEM_EXIT":
        log.info("Script exited with code %s.", outcome.exit_code)
    elif outcome.status.name == "KEYBOARD_INTERRUPT":
        log.warning("Script interrupted (KeyboardInterrupt).")
    else:
        log.error("Script terminated abnormally: %s", outcome.detail or "unknown error")


def add_run_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "run",
        help=(
            "instrument one or more functions/methods and execute a Python script "
            "(use -- to separate targets from the script)"
        ),
    )
    # No hard-coded defaults: layered config fills missing values
    p.add_argument("-l", "--limit", type=int, default=argparse.SUPPRESS)
    p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--format", choices=["pickle", "json", "repr", "graph-json"], default=argparse.SUPPRESS)
    p.add_argument(
        "--additional-sys-path",
        dest="additional_sys_path",
        nargs="*",
        default=argparse.SUPPRESS,
        help="extra import roots (relative paths are anchored on the project root)",
    )

    p.add_argument(
        "targets",
        nargs="*",
        default=argparse.SUPPRESS,
        metavar="target",
        help="one or more targets: 'module.function' or 'module.Class.method'",
    )
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the Python script to run (with arguments)")
    p.set_defaults(handler=_handle)

