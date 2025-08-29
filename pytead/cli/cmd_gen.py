# pytead/cli/cmd_gen.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from ..errors import PyteadError
from ..logconf import configure_logger
from ._cli_utils import emptyish, resolve_output_paths, ensure_storage_dir_or_exit
from .config_cli import load_layered_config, apply_effective_to_args
from . import service_cli as svc


def _handle(args: argparse.Namespace) -> None:
    """
    Thin handler: resolve config/args, check storage dir, delegate to service layer.
    """
    log = configure_logger(name="pytead.cli.gen")

    # Load layered config (packaged < user < project) and fill missing/empty args
    ctx = load_layered_config()
    apply_effective_to_args("gen", ctx, args)

    # Resolve output policy: if neither -o nor -d, use a single default file
    output, output_dir = resolve_output_paths(
        getattr(args, "output", None),
        getattr(args, "output_dir", None),
        Path("tests/test_pytead_generated.py"),
    )

    # Validate + resolve storage dir (with a detailed diagnostic on failure)
    storage_dir = ensure_storage_dir_or_exit(ctx, "gen", getattr(args, "storage_dir", None), log)

    # Import roots to embed in generated tests (used by testkit.setup at runtime)
    gen_extra: List[str] = getattr(args, "additional_sys_path", None) or []
    import_roots = ["."]
    import_roots += [str(p) for p in gen_extra]

    # Delegate to service layer: collect traces + emit tests (single file or per function)
    try:
        result = svc.collect_and_emit_tests(
            storage_dir=storage_dir,
            formats=getattr(args, "formats", None),
            output=output,
            output_dir=Path(output_dir) if output_dir else None,
            import_roots=import_roots,
            logger=log,
        )
    except PyteadError:
        raise
    except Exception as exc:
        log.error("Generation failed: %s", exc)
        raise

    if result is None:
        # No traces or filter excluded everything
        log.warning("No tests generated.")
        return

    # Summary
    if result.output_dir:
        log.info(
            "Generated %d test module(s) in '%s' (%d unique cases).",
            result.files_written, result.output_dir, result.unique_cases
        )
    else:
        log.info(
            "Generated '%s' with %d unique case(s).",
            result.output, result.unique_cases
        )


def add_gen_subparser(subparsers) -> None:
    p = subparsers.add_parser("gen", help="generate pytest tests from traces")

    p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS,
                   help="directory containing trace files (defaults via layered config)")

    grp = p.add_mutually_exclusive_group()
    grp.add_argument("-o", "--output", type=Path, default=argparse.SUPPRESS,
                     help="single-file output for generated tests")
    grp.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS,
                     help="write one test module per function in this directory")

    p.add_argument("--formats", choices=["pickle", "graph-json"], nargs="*", default=argparse.SUPPRESS)
    # optional: allow additional roots via config (mirrors run/tead)
    p.add_argument("--additional-sys-path", dest="additional_sys_path", nargs="*", default=argparse.SUPPRESS)

    p.set_defaults(handler=_handle)

