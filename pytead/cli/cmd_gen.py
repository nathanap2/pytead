# pytead/cli/cmd_gen.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from . import service_cli as svc
from ._common import (
    make_logger,
    load_ctx_anchored,
    eff,
    norm_roots,
    storage_for_read,
    pick_output_dir,
    add_opt_storage_dir,
    add_opt_formats,
    add_opt_output_dir,
    add_opt_additional_sys_path,
)


def _handle(args: argparse.Namespace) -> None:
    log = make_logger("gen")

    ctx = load_ctx_anchored("gen", args, anchor=None)
    eff_gen = eff(ctx, "gen")

    storage_dir: Path = storage_for_read(ctx, getattr(args, "storage_dir", None), log, "gen")
    output_dir: Path = pick_output_dir(ctx, getattr(args, "output_dir", None), [eff_gen], log, section="gen")

    project_root = Path(ctx.project_root)
    abs_roots, _paths = norm_roots(project_root, getattr(args, "additional_sys_path", None))
    import_roots: Optional[list[str]] = abs_roots

    svc.collect_and_emit_tests(
        storage_dir=storage_dir,
        formats=list(getattr(args, "formats", []) or []),
        output_dir=output_dir,
        import_roots=import_roots,
        logger=log,
    )


def add_gen_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("gen", help="generate pytest tests from traces")

    add_opt_storage_dir(p, for_read=True)
    add_opt_output_dir(p)
    add_opt_formats(p)
    add_opt_additional_sys_path(p)

    p.set_defaults(handler=_handle)

