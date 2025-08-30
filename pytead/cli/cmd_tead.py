# pytead/cli/cmd_tead.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from ..logconf import configure_logger
from .config_cli import (
    load_layered_config,
    apply_effective_to_args,
    effective_section,
)
from ._cli_utils import (
    split_targets_and_cmd,
    fallback_targets_from_cfg,
    first_py_token,
    resolve_under,
    require_script_py_or_exit,
)
from . import service_cli as svc


log = configure_logger(name="pytead.cli.tead")


def _as_path(x: Optional[Path | str]) -> Optional[Path]:
    if x is None:
        return None
    return x if isinstance(x, Path) else Path(x)


def run(args: argparse.Namespace) -> None:
    """
    Combined command: instrument+run, then generate tests immediately.

    Behavior:
      - Supports the common mistake where the script .py is passed among `targets`
        (handled by split_targets_and_cmd).
      - Loads layered config from the first '*.py' token in cmd/targets.
      - Anchors storage_dir, out_dir and additional_sys_path under project_root.
      - Fallback chain for `targets` and `out_dir` using [tead]/[run]/[gen].
    """
    # --- 0) Split positionals
    raw_targets = getattr(args, "targets", []) or []
    raw_cmd = getattr(args, "cmd", []) or []
    targets, cmd = split_targets_and_cmd(raw_targets, raw_cmd)

    # Hint for config discovery
    start_hint = first_py_token(cmd)

    # --- 1) Layered config → args
    ctx = load_layered_config(start=start_hint)
    apply_effective_to_args("tead", ctx, args)

    eff_tead = effective_section(ctx, "tead") or {}
    eff_run = effective_section(ctx, "run") or {}
    eff_gen = effective_section(ctx, "gen") or {}

    # Targets: CLI → [tead].targets → [run].targets
    targets = fallback_targets_from_cfg(targets, eff_tead, log, "TEAD")
    if not targets:
        targets = fallback_targets_from_cfg(targets, eff_run, log, "RUN")
    if not targets:
        log.error(
            "No target provided. Expect at least one 'module.function' or 'module.Class.method'. "
            "Config file used: %s",
            str(getattr(ctx, "source_path", None) or "<none>"),
        )
        sys.exit(1)

    # Require a Python script after '--'
    script_path = require_script_py_or_exit(cmd, log)

    # --- 2) Path anchoring under project_root
    project_root = Path(ctx.project_root)

    storage_dir = _as_path(getattr(args, "storage_dir"))
    storage_dir = resolve_under(project_root, storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    out_dir = (
        getattr(args, "output_dir", None)
        or eff_tead.get("out_dir")
        or eff_gen.get("out_dir")
    )
    out_dir = resolve_under(project_root, _as_path(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    # additional_sys_path : CLI → [tead] → [run]
    extra_paths = (
        getattr(args, "additional_sys_path", None)
        or eff_tead.get("additional_sys_path")
        or eff_run.get("additional_sys_path")
        or []
    )
    add_paths: List[Path] = [resolve_under(project_root, _as_path(p)) for p in extra_paths]

    # --- 3) Instrument + run
    instr, outcome, roots = svc.instrument_and_run(
        targets=targets,
        limit=getattr(args, "limit"),
        storage_dir=storage_dir,
        storage=getattr(args, "format"),
        script_file=script_path,
        argv=cmd,
        additional_sys_path=add_paths,
        logger=log,
    )

    # --- 4) Generate tests immediately
    gen_formats = getattr(args, "gen_formats", None) or [getattr(args, "format")]
    only_targets = targets if getattr(args, "only_targets", False) else None

    svc.collect_and_emit_tests(
        storage_dir=storage_dir,
        formats=gen_formats,
        output_dir=out_dir,
        import_roots=roots,
        only_targets=only_targets,
        logger=log,
    )


def add_tead_subparser(subparsers) -> None:
    p = subparsers.add_parser("tead", help="trace and immediately generate tests (all-in-one)")

    # Capture options (mirror 'run')
    p.add_argument("-l", "--limit", type=int, default=argparse.SUPPRESS,
                   help="max calls to record per function")
    p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS,
                   help="directory to write trace files (defaults via layered config)")
    p.add_argument("--format", choices=["pickle", "graph-json"], default=argparse.SUPPRESS,
                   help="trace format to write (default via layered config)")

    # Generation options (mirror 'gen')
    p.add_argument("--gen-formats", choices=["pickle", "graph-json"], nargs="*", default=argparse.SUPPRESS)
    p.add_argument("--only-targets", action="store_true", default=argparse.SUPPRESS,
                   help="only emit tests for the provided targets (skip other traced functions)")
    p.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS)

    # Import roots (shared policy with run/gen)
    p.add_argument("--additional-sys-path", dest="additional_sys_path", nargs="*", default=argparse.SUPPRESS)

    # Positionals: targets then the script after '--'
    p.add_argument("targets", nargs="*", default=argparse.SUPPRESS,
                   metavar="target",
                   help="one or more targets: 'module.function' or 'module.Class.method'")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="-- then the Python script to run (with its arguments)")

    p.set_defaults(handler=run)

