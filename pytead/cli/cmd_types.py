# pytead/cli/cmd_types.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from ..errors import PyteadError
from ..logconf import configure_logger
from .config_cli import effective_section
from . import service_cli as svc  # keep your existing service entrypoints
from ._cli_utils import (
    load_ctx_and_fill,
    ensure_storage_dir_or_exit,
    require_output_dir_or_exit,
    normalize_additional_sys_path,
)


def _handle(args: argparse.Namespace) -> None:
    log = configure_logger(name="pytead.cli.types")

    # 1) Load layered config; anchor on storage_dir or output_dir if provided
    ctx = load_ctx_and_fill(
        "types",
        args,
        lambda a: getattr(a, "storage_dir", None) or getattr(a, "output_dir", None),
    )
    eff_types = effective_section(ctx, "types") or {}
    project_root = Path(ctx.project_root)

    # 2) Resolve and validate the input directory (traces)
    storage_dir: Path = ensure_storage_dir_or_exit(ctx, "types", getattr(args, "storage_dir", None), log)

    # 3) Resolve the output directory (where `.pyi` files are written)
    out_value: Optional[Path | str] = (
        getattr(args, "output_dir", None)
        or eff_types.get("output_dir")
    )
    output_dir: Path = require_output_dir_or_exit(ctx, out_value, log, section="types")

    # 4) Normalize additional import roots (absolute, as strings)
    extra_abs = normalize_additional_sys_path(project_root, getattr(args, "additional_sys_path", None))
    import_roots = extra_abs or None  # service layer usually accepts Optional[Iterable[str]]

    # 5) Delegate to your domain-specific stub emission logic
    try:
        # If you already have a dedicated function for stub generation, call it here.
        # Example (adjust to your actual service API):
        #
        # svc.collect_and_emit_type_stubs(
        #     storage_dir=storage_dir,
        #     formats=getattr(args, "formats", None),  # typically ["pickle"]
        #     output_dir=output_dir,
        #     import_roots=import_roots,
        #     logger=log,
        # )
        pass
    except PyteadError:
        # Let main() handle uniform error reporting for PyteadError
        raise


def add_types_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `types` subcommand."""
    p = subparsers.add_parser("types", help="generate .pyi type stubs from traces")

    # Input (traces)
    p.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing recorded traces (defaults via layered config)",
    )

    # Output (.pyi destination)
    p.add_argument(
        "-d",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="write stub files (.pyi) under this directory (required via CLI or config)",
    )

    # Restrict input formats (only 'pickle' is supported here)
    p.add_argument(
        "--formats",
        choices=["pickle"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict formats when reading traces (default: pickle)",
    )

    # Additional import roots (same policy as run/gen/tead)
    p.add_argument(
        "--additional-sys-path",
        dest="additional_sys_path",
        nargs="*",
        default=argparse.SUPPRESS,
        help="extra import roots; relative paths are resolved under the project root",
    )

    p.set_defaults(handler=_handle)

