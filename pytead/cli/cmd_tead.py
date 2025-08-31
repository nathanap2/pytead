# pytead/cli/cmd_tead.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from . import service_cli as svc
from ._cli_utils import extract_script_and_argv
from . import _common

def _handle(args: argparse.Namespace) -> None:
    log = _common.make_logger("tead")

    script_path, argv = extract_script_and_argv(list(getattr(args, "cmd", []) or []), log)

    ctx = _common.load_ctx_anchored("tead", args, script_path)

    eff_tead = _common.eff(ctx, "tead")
    eff_run  = _common.eff(ctx, "run")
    eff_gen  = _common.eff(ctx, "gen")

    targets: List[str] = _common.compute_targets(getattr(args, "targets", None), [eff_tead, eff_run], log, "TEAD")
    if not targets:
        log.warning("No target provided; nothing to instrument/generate.")
        return

    storage_dir: Path = _common.storage_for_write(ctx, getattr(args, "storage_dir", None), log)
    out_dir: Path = _common.pick_output_dir(ctx, getattr(args, "output_dir", None), [eff_tead, eff_run, eff_gen], log, section="tead")

    project_root = Path(ctx.project_root)
    abs_roots, add_paths = _common.norm_roots(project_root, getattr(args, "additional_sys_path", None))

    fmt = getattr(args, "format", None)

    outcome = svc.instrument_and_run(
        targets=targets,
        limit=getattr(args, "limit", None),
        storage_dir=storage_dir,
        storage=fmt,
        script_file=script_path,
        argv=argv,
        additional_sys_path=add_paths,
        project_root=project_root,
        logger=log,
    )

    # TEAD drives GEN with [format] as the only generation format
    gen_formats: List[str] = [fmt] if fmt is not None else []

    only_targets: Optional[List[str]] = targets

    svc.collect_and_emit_tests(
        storage_dir=storage_dir,
        formats=gen_formats,
        output_dir=out_dir,
        import_roots=abs_roots,
        only_targets=only_targets,
        logger=log,
    )


def add_tead_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("tead", help="instrument & run a script, then generate tests")

    p.add_argument(
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="max number of calls to capture per target (default from config)",
    )

    _common.add_opt_format(p)

    _common.add_opt_storage_dir(p, for_read=False)
    _common.add_opt_output_dir(p)

    _common.add_opt_additional_sys_path(p)
    _common.add_opt_targets(p)

    _common.add_opt_cmd_remainder(p)

    p.set_defaults(handler=_handle)

