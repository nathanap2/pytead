# pytead/cli/cmd_types.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List

from ..logconf import configure_logger
from .config_cli import load_layered_config, apply_effective_to_args, effective_section
from ..gen_tests import collect_entries
from .._cases import unique_cases
from ..gen_types import summarize_function_types, group_by_module, render_stub_module


def _as_path(x) -> Path | None:
    if x is None:
        return None
    return Path(x)


def _resolve_under(root: Path, p: Path | None) -> Path | None:
    """Return absolute path under `root` when `p` is relative; passthrough if absolute/None."""
    if p is None:
        return None
    return p if p.is_absolute() else (root / p)


def _detect_project_root(ctx, explicit: Path | None) -> Path:
    """
    Decide a stable project root:
    - if user passed --project-root, prefer it,
    - else use the root discovered by the layered config (ctx.project_root),
    - fallback to CWD.
    """
    if explicit is not None:
        return explicit.resolve()
    if getattr(ctx, "project_root", None):
        try:
            return Path(ctx.project_root).resolve()
        except Exception:
            pass
    return Path.cwd().resolve()


def _load_effective_types_config(args: argparse.Namespace, start: Path | None):
    """
    Load layered config and fill args fields that are missing. Then read the effective
    [types] section so we can map storage_dir -> calls_dir.
    """
    ctx = load_layered_config(start=start)
    apply_effective_to_args("types", ctx, args)
    eff_types = effective_section(ctx, "types")
    return ctx, eff_types


def _finalize_io_paths(project_root: Path, calls_dir: Path | None, out_dir: Path | None, eff_types: dict):
    if calls_dir is None:
        cfg_calls = eff_types.get("storage_dir")
        calls_dir = _as_path(cfg_calls)
    if out_dir is None:
        cfg_out = eff_types.get("out_dir")
        out_dir = _as_path(cfg_out)
    calls_dir = _resolve_under(project_root, calls_dir) if calls_dir else None
    out_dir = _resolve_under(project_root, out_dir) if out_dir else None
    return calls_dir, out_dir



def _require(cond: bool, log, msg: str) -> None:
    if not cond:
        log.error(msg)
        sys.exit(1)


def _insert_project_on_sys_path(project_root: Path) -> None:
    """Ensure the project root is importable for type summarization."""
    s = str(project_root)
    if s not in sys.path:
        sys.path.insert(0, s)


def _summarize_entries_by_func(
    log, entries_by_func: Dict[str, List[dict]]
) -> Dict[str, any]:
    typed_infos: Dict[str, any] = {}
    for func, entries in entries_by_func.items():
        samples = [{"args": a, "kwargs": k, "result": r} for (a, k, r) in unique_cases(entries)]
        if not samples:
            continue
        try:
            info = summarize_function_types(func, samples)  # falls back if import fails
            typed_infos[func] = info
        except Exception as exc:
            log.warning("Type inference failed for %s: %s", func, exc)
    return typed_infos


def _emit_stubs(grouped, out_dir: Path, log) -> int:
    written = 0
    for module, funcs in grouped.items():
        rel = Path(*module.split("."))
        dest = out_dir / rel.with_suffix(".pyi")
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = render_stub_module(module, funcs)
        dest.write_text(src, encoding="utf-8")
        written += 1
        log.info("Wrote stub for module %s → %s", module, dest)
    return written


def _handle(args: argparse.Namespace) -> None:
    log = configure_logger(name="pytead.cli.types")

    # Optional user hint for config discovery
    explicit_root = _as_path(getattr(args, "project_root", None))
    ctx, eff_types = _load_effective_types_config(args, start=explicit_root)

    # Decide final project root and anchor paths under it
    project_root = _detect_project_root(ctx, explicit_root)
    calls_dir = _as_path(getattr(args, "calls_dir", None))
    out_dir = _as_path(getattr(args, "out_dir", None))
    formats = getattr(args, "formats", None)

    calls_dir, out_dir = _finalize_io_paths(project_root, calls_dir, out_dir, eff_types)

    _require(calls_dir is not None and out_dir is not None,
             log, "You must provide both --calls-dir and --out-dir (or set them in [types]).")

    _require(calls_dir.exists() and calls_dir.is_dir(),
             log, f"Calls directory '{calls_dir}' does not exist or is not a directory")

    # Ensure imports from the project resolve during summarization
    _insert_project_on_sys_path(project_root)

    # Collect traces and summarize type information
    entries_by_func: Dict[str, List[dict]] = collect_entries(storage_dir=calls_dir, formats=formats)
    if not entries_by_func:
        log.error("No traces found in '%s' (formats=%s).", calls_dir, formats or "auto")
        sys.exit(1)

    typed_infos = _summarize_entries_by_func(log, entries_by_func)
    if not typed_infos:
        log.error("No usable samples for type inference.")
        sys.exit(1)

    grouped = group_by_module(typed_infos)
    written = _emit_stubs(grouped, out_dir, log)
    log.info("Generated %d stub module(s) into '%s'.", written, out_dir)


def add_types_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "types", help="infer types from traces and emit .pyi stub files"
    )
    p.add_argument(
        "-c",
        "--calls-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing trace files (if omitted, uses [types].storage_dir)",
    )
    p.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="output directory root for .pyi files (if omitted, uses [types].out_dir)",
    )
    p.add_argument(
        "--formats",
        choices=["pickle"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict formats when reading traces",
    )
    p.add_argument(
        "--project-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="project root to put on sys.path and to anchor relative paths (default: detected)",
    )
    p.set_defaults(handler=_handle)

