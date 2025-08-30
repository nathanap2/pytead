# pytead/cli/cmd_gen.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from ..errors import PyteadError
from ..logconf import configure_logger
from ._cli_utils import ensure_storage_dir_or_exit
from .config_cli import load_layered_config, apply_effective_to_args
from . import service_cli as svc


def _handle(args: argparse.Namespace) -> None:
    """
    Generate **one test file per function**.
    No implicit defaults here: the caller/config MUST provide `--output-dir` (or [gen].output_dir).
    """
    log = configure_logger(name="pytead.cli.gen")

    # Load layered config and fill missing args from the effective [gen] section.
    ctx = load_layered_config()
    apply_effective_to_args("gen", ctx, args)

    # Validate storage dir (with detailed diagnostics on failure).
    storage_dir = ensure_storage_dir_or_exit(ctx, "gen", getattr(args, "storage_dir", None), log)

    # Require an explicit output directory (CLI or config).
    out_dir = getattr(args, "output_dir", None)
    if out_dir is None:
        log.error("`pytead gen` requires --output-dir or [gen].output_dir in config (no implicit defaults).")
        import sys as _sys
        _sys.exit(1)
    if not isinstance(out_dir, Path):
        out_dir = Path(out_dir)

    # Optional import roots to embed; if not provided, pass None (no implicit defaults here).
    gen_extra = getattr(args, "additional_sys_path", None)
    import_roots = list(gen_extra) if gen_extra else None

    # Delegate to the service layer.
    try:
        result = svc.collect_and_emit_tests(
            storage_dir=storage_dir,
            formats=getattr(args, "formats", None),
            output_dir=out_dir,
            import_roots=import_roots,
            logger=log,
        )
    except PyteadError:
        raise
    except Exception as exc:
        log.error("Generation failed: %s", exc)
        raise

    if result is None:
        # No traces or filters excluded everything.
        log.warning("No tests generated.")
        return

    # Summary
    log.info(
        "Generated %d test module(s) in '%s' (%d unique cases).",
        result.files_written, result.output_dir, result.unique_cases
    )



def add_gen_subparser(subparsers) -> None:
    p = subparsers.add_parser("gen", help="generate pytest tests from traces")

    p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS,
                   help="directory containing trace files (defaults via layered config)")

    p.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS,
                   help="write one test module per function into this directory (default: tests/generated)")


    p.add_argument("--formats", choices=["pickle", "graph-json"], nargs="*", default=argparse.SUPPRESS)
    # optional: allow additional roots via config (mirrors run/tead)
    p.add_argument("--additional-sys-path", dest="additional_sys_path", nargs="*", default=argparse.SUPPRESS)

    p.set_defaults(handler=_handle)

