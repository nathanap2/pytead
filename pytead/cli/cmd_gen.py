# pytead/cli/cmd_gen.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Union

from ..gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from ..logconf import configure_logger
from ._cli_utils import unique_count
from .config_cli import load_layered_config, apply_effective_to_args, diagnostics_for_storage_dir


def _emptyish(x) -> bool:
    """Treat None, '' and empty list/dict as 'absent'."""
    return x is None or (isinstance(x, (str, list, dict)) and len(x) == 0)


def _handle(args: argparse.Namespace) -> None:
    """Generate pytest tests from recorded traces."""
    logger = configure_logger(name="pytead.cli.gen")

    # Layered config (packaged < user < project)
    ctx = load_layered_config()
    apply_effective_to_args("gen", ctx, args)

    storage_dir = getattr(args, "storage_dir", None)
    output = getattr(args, "output", None)
    output_dir = getattr(args, "output_dir", None)
    formats = getattr(args, "formats", None)

    # Resolve output destination: CLI -o/-d > [gen].output_dir > default single file.
    if _emptyish(output) and _emptyish(output_dir):
        # If config didn't provide [gen].output_dir, default to one file.
        output = Path("tests/test_pytead_generated.py")

    # Validations
    if _emptyish(storage_dir):
        logger.error(
            "GEN: missing 'storage_dir'. Ensure it exists in [defaults] or [gen], "
            "or pass --storage-dir. Config used: %s",
            str(ctx.source_path) if ctx.source_path else "<none>",
        )
        # Extra diagnostics to help understand user's environment
        try:
            diag = diagnostics_for_storage_dir(ctx, "gen", storage_dir)
            logger.error("\n%s", diag)
        except Exception:
            pass
        sys.exit(1)

    storage_dir = Path(storage_dir)
    if not storage_dir.exists() or not storage_dir.is_dir():
        logger.error(
            "Storage (calls) directory '%s' does not exist or is not a directory",
            storage_dir,
        )
        try:
            # Show where it *should* be after anchoring under project_root
            diag = diagnostics_for_storage_dir(ctx, "gen", storage_dir)
            logger.error("\n%s", diag)
        except Exception:
            pass
        sys.exit(1)

    # Collect traces and render tests
    entries = collect_entries(storage_dir=storage_dir, formats=formats)
    total_unique = unique_count(entries)

    # Prepare import header in generated tests to support modules outside the root.
    gen_extra = getattr(args, "additional_sys_path", None) or []
    import_roots = ["."]
    import_roots += [str(p) for p in gen_extra]

    if not _emptyish(output_dir):
        write_tests_per_func(entries, Path(output_dir), import_roots=import_roots)
        logger.info(
            "Generated %d test modules in '%s' (%d total unique tests)",
            len(entries),
            output_dir,
            total_unique,
        )
    else:
        source = render_tests(entries, import_roots=import_roots)
        write_tests(source, Path(output))
        logger.info("Generated '%s' with %d unique tests", output, total_unique)


def add_gen_subparser(subparsers) -> None:
    p_gen = subparsers.add_parser("gen", help="generate pytest tests from traces")

    p_gen.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing trace files (defaults via layered config)",
    )

    group = p_gen.add_mutually_exclusive_group()
    group.add_argument(
        "-o",
        "--output",
        type=Path,
        default=argparse.SUPPRESS,
        help="single-file output for generated tests",
    )
    group.add_argument(
        "-d",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="write one test module per function in this directory",
    )

    p_gen.add_argument(
        "--formats",
        choices=["pickle", "json", "repr"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict to these formats when reading (default: all present)",
    )
    # Note: no CLI flag for additional_sys_path; pass it via config:
    # [gen].additional_sys_path = ["src", "third_party/pkg", ...]
    p_gen.set_defaults(handler=_handle)

