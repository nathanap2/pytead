# pytead/cli/cmd_run.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from . import service_cli as svc
from ._cli_utils import extract_script_and_argv
from ._common import (
    make_logger,
    load_ctx_anchored,
    eff,
    norm_roots,
    storage_for_write,
    compute_targets,
    add_opt_storage_dir,
    add_opt_format,
    add_opt_additional_sys_path,
    add_opt_targets,
    add_opt_cmd_remainder,
)


def _handle(args: argparse.Namespace) -> None:
    log = make_logger("run")

    # 1) Script + argv (supports the `--` sentinel)
    script_path, argv = extract_script_and_argv(list(getattr(args, "cmd", []) or []), log)

    # 2) Load layered config anchored on the script location; hydrate [run] into args
    ctx = load_ctx_anchored("run", args, script_path)

    # 3) Targets: CLI → [run].targets (no implicit defaults beyond that)
    eff_run = eff(ctx, "run")
    targets: List[str] = compute_targets(getattr(args, "targets", None), [eff_run], log, "RUN")
    if not targets:
        log.warning("No target provided; nothing to instrument.")
        return

    # 4) Resolve storage_dir for write mode (mkdir if needed)
    storage_dir: Path = storage_for_write(ctx, getattr(args, "storage_dir", None), log)

    # 5) Additional import roots (absolute); service expects Paths here
    project_root = Path(ctx.project_root)
    _abs_roots, add_paths = norm_roots(project_root, getattr(args, "additional_sys_path", None))

    # 6) Orchestrate: instrument → run (no exit-code mapping, let exceptions bubble)
    svc.instrument_and_run(
        targets=targets,
        limit=getattr(args, "limit", None),
        storage_dir=storage_dir,
        storage=getattr(args, "format", None),
        script_file=script_path,
        argv=argv,
        additional_sys_path=add_paths,
        project_root=project_root,
        logger=log,
    )


def add_run_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("run", help="instrument targets then run a Python script")

    p.add_argument(
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="max number of calls to capture per target (default from config)",
    )

    add_opt_storage_dir(p, for_read=False)
    add_opt_format(p)
    add_opt_additional_sys_path(p)
    add_opt_targets(p)
    add_opt_cmd_remainder(p)

    p.set_defaults(handler=_handle)

