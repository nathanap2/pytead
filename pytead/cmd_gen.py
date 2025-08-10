# pytead/pytead/cmd_gen.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List

from .gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from .logconf import configure_logger
from ._cli_utils import unique_count
from .config import apply_config_from_default_file


def _handle(args: argparse.Namespace) -> None:
    """Generate pytest tests from recorded traces."""
    logger = configure_logger(name="pytead.cli.gen")

    # Fill from config
    apply_config_from_default_file("gen", args)

    # Validate required options (no in-code defaults)
    calls_dir = getattr(args, "calls_dir", None)
    output = getattr(args, "output", None)
    output_dir = getattr(args, "output_dir", None)
    formats = getattr(args, "formats", None)

    if calls_dir is None:
        logger.error(
            "Missing 'calls_dir' for 'gen'. Please set [gen].calls_dir in .pytead/config.toml or pass -c/--calls-dir."
        )
        sys.exit(1)
    if output is None and output_dir is None:
        logger.error(
            "You must set either [gen].output or [gen].output_dir in .pytead/config.toml or pass -o/--output or -d/--output-dir."
        )
        sys.exit(1)

    calls_dir = Path(calls_dir)
    if not calls_dir.exists() or not calls_dir.is_dir():
        logger.error("Calls directory '%s' does not exist or is not a directory", calls_dir)
        sys.exit(1)

    entries: Dict[str, List[dict]] = collect_entries(calls_dir=calls_dir, formats=formats)
    total_unique = unique_count(entries)

    if output_dir is not None:
        write_tests_per_func(entries, Path(output_dir))
        logger.info(
            "Generated %d test modules in '%s' (%d total unique tests)",
            len(entries),
            output_dir,
            total_unique,
        )
    else:
        source = render_tests(entries)
        write_tests(source, Path(output))
        logger.info("Generated '%s' with %d unique tests", output, total_unique)


def add_gen_subparser(subparsers) -> None:
    p_gen = subparsers.add_parser("gen", help="generate pytest tests from traces")
    p_gen.add_argument("-c", "--calls-dir", type=Path, default=argparse.SUPPRESS,
                       help="directory containing trace files")
    group = p_gen.add_mutually_exclusive_group()
    group.add_argument("-o", "--output", type=Path, default=argparse.SUPPRESS,
                       help="single-file output for generated tests")
    group.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS,
                       help="write one test module per function in this directory")
    p_gen.add_argument("--formats", choices=["pickle", "json", "repr"], nargs="*",
                       default=argparse.SUPPRESS, help="restrict to these formats when reading")
    p_gen.set_defaults(handler=_handle)

