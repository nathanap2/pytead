# pytead/cli/cmd_types.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict

from ..logconf import configure_logger
from .config_cli import (
    load_layered_config,
    apply_effective_to_args,
    effective_section,
    resolve_under_project_root,
)
from ._cli_utils import ensure_storage_dir_or_exit
from ..gen_tests import collect_entries
from .._cases import unique_cases
from ..gen_types import summarize_function_types, group_by_module, render_stub_module


log = configure_logger(name="pytead.cli.types")


def _module_to_rel_path(module: str) -> Path:
    """Convert dotted module name to a relative path (no suffix)."""
    return Path(*module.split("."))


def _handle(args: argparse.Namespace) -> None:
    """
    Read recorded traces and emit `.pyi` stubs grouped by module.

    Policy:
      - CLI overrides layered config ([types]).
      - storage_dir is validated with detailed diagnostics.
      - output_dir is required (CLI or [types].out_dir) and anchored on project_root.
      - Minimal logic here; heavy lifting is delegated to core libs.
    """
    # Optional hint for config discovery and path anchoring
    explicit_root = getattr(args, "project_root", None)
    ctx = load_layered_config(start=explicit_root)

    # Fill args from effective [types]; keep CLI precedence
    apply_effective_to_args("types", ctx, args)
    eff_types: Dict = effective_section(ctx, "types") or {}

    # --- Resolve/validate storage dir (reuses common helper with rich diagnostics)
    storage_dir = ensure_storage_dir_or_exit(ctx, "types", getattr(args, "storage_dir", None), log)

    # --- Resolve output directory (required)
    out_dir = getattr(args, "output_dir", None) or eff_types.get("out_dir")
    if out_dir is None:
        log.error("`pytead types` requires --output-dir or [types].out_dir in config (no implicit defaults).")
        sys.exit(1)
    out_dir = resolve_under_project_root(ctx, out_dir)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # Ensure project root is importable (if downstream code needs imports)
    pr = Path(ctx.project_root)
    s = str(pr)
    if s not in sys.path:
        sys.path.insert(0, s)

    # Formats: default to 'pickle' (only supported here)
    formats = getattr(args, "formats", None) or ["pickle"]

    # 1) Collect entries from traces
    entries = collect_entries(storage_dir=storage_dir, formats=formats)
    if not entries:
        log.warning("No trace entries found in %s (formats=%s). Nothing to do.", storage_dir, formats)
        return

    # 2) Deduplicate/normalize cases
    entries = unique_cases(entries)

    # 3) Summarize types per function
    func_summaries = summarize_function_types(entries)

    # 4) Group by module and render .pyi files
    grouped = group_by_module(func_summaries)
    written = 0
    for module, funcs in grouped.items():
        rel = _module_to_rel_path(module)
        dest = Path(out_dir) / rel.with_suffix(".pyi")
        dest.parent.mkdir(parents=True, exist_ok=True)

        src = render_stub_module(module, funcs)
        dest.write_text(src, encoding="utf-8")
        written += 1
        log.info("Wrote stub for module %s → %s", module, dest)

    if written == 0:
        log.warning("No stubs were generated (grouping was empty).")
    else:
        log.info("Generated %d stub module(s) into %s.", written, out_dir)


def add_types_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "types",
        help="read traces and generate .pyi stubs grouped by module",
    )

    # Input traces (validated via helper; may also come from [types].storage_dir)
    p.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing trace files (defaults via layered config)",
    )

    # Output stubs (required via CLI or [types].out_dir)
    p.add_argument(
        "-d",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="write stub files (.pyi) under this directory (defaults via layered config)",
    )

    # Restrict input formats (only 'pickle' supported here)
    p.add_argument(
        "--formats",
        choices=["pickle"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict formats when reading traces (default: pickle)",
    )

    # Optional hint for config discovery and path anchoring
    p.add_argument(
        "--project-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="project root to put on sys.path and to anchor relative paths (default: detected)",
    )

    p.set_defaults(handler=_handle)

