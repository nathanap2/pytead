import argparse
import importlib
import runpy
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple

from .tracing import trace
from .gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from .storage import get_storage, storages_from_names
from .clean import _plan_deletions
from .logconf import configure_logger
from .config import (
    apply_config_from_default_file,
    get_effective_config,
    LAST_CONFIG_PATH,
)
from ._cli_utils import split_targets_and_cmd, unique_count, fallback_targets_from_cfg

def run(args) -> None:
    """End-to-end: trace targets while running a script, then generate pytest tests."""
    logger = configure_logger(name="pytead.tead")

    # 0) Show raw args as parsed by argparse (before config injection)
    logger.info(
        "TEAD: raw args (pre-config): %s", {k: getattr(args, k) for k in vars(args)}
    )

    # 1) Fill from default_config.toml (does NOT override explicit CLI flags)
    apply_config_from_default_file("tead", args)

    # 1bis) Last-resort defaults (only if neither CLI nor config provided them)
    box = vars(args)
    if "limit" not in box:
        args.limit = 10
    if "storage_dir" not in box:
        args.storage_dir = Path("call_logs")
    if "format" not in box:
        args.format = "pickle"
    if "calls_dir" not in box or getattr(args, "calls_dir", None) is None:
        args.calls_dir = args.storage_dir
    if "gen_formats" not in box or not getattr(args, "gen_formats", None):
        args.gen_formats = [args.format]
    if "pre_clean" not in box:
        args.pre_clean = False
    if "pre_clean_before" not in box:
        args.pre_clean_before = None
    if "only_targets" not in box:
        args.only_targets = False

    # 2) Resolve absolute paths from the original cwd
    caller_cwd = Path.cwd().resolve()
    storage_dir_abs = Path(args.storage_dir).resolve()
    calls_dir_abs = (
        Path(args.calls_dir).resolve() if args.calls_dir else storage_dir_abs
    )

    # Snapshot effective config for potential fallback of targets
    effective_cfg = get_effective_config("tead")
    logger.info("TEAD: effective config snapshot: %s", effective_cfg or "{}")

    # 3) Targets & command (robust split; '.py' tokens move to cmd)
    targets, cmd = split_targets_and_cmd(
        getattr(args, "targets", []) or [], getattr(args, "cmd", [])
    )
    logger.info("TEAD: split targets=%s cmd=%s", targets, cmd)

    # Fallback: if targets ended up empty (e.g., a *.py was moved to cmd), let config supply them.
    targets = fallback_targets_from_cfg(targets, effective_cfg, logger, "TEAD")

    if not targets:
        logger.error(
            "No target provided. Expect at least one 'module.function'. Config file used: %s",
            LAST_CONFIG_PATH,
        )
        sys.exit(1)
    if not cmd:
        logger.error("No script specified after '--'")
        sys.exit(1)

    sys.path.insert(0, str(caller_cwd))
    storage = get_storage(args.format)

    # 4) Optional pre-clean (in ABSOLUTE calls dir)
    if args.pre_clean:
        to_delete, total_bytes = _plan_deletions(
            calls_dir=calls_dir_abs,
            formats=[args.format],
            functions=targets,
            glob_patterns=[],
            before_iso=args.pre_clean_before,
            logger=logger,
        )
        for p in to_delete:
            try:
                p.unlink()
            except Exception as exc:
                logger.warning("Failed to remove %s: %s", p, exc)
        if to_delete:
            logger.info(
                "Pre-clean removed %d file(s) (~%.1f KB) from %s.",
                len(to_delete),
                total_bytes / 1024.0,
                calls_dir_abs,
            )

    # 5) Instrumentation (write to ABSOLUTE storage_dir)
    resolved: List[Tuple[Any, str, str]] = []
    errors: List[str] = []
    for t in targets:
        try:
            module_name, func_name = t.rsplit(".", 1)
        except ValueError:
            errors.append(f"Invalid target '{t}': expected format module.function")
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(
                f"Cannot import module '{module_name}' for target '{t}': {exc}"
            )
            continue
        if not hasattr(module, func_name):
            errors.append(
                f"Function '{func_name}' not found in module '{module_name}' (target '{t}')"
            )
            continue
        resolved.append((module, module_name, func_name))

    if errors:
        for m in errors:
            logger.error(m)
        sys.exit(1)

    seen = set()
    for module, module_name, func_name in resolved:
        key = (module_name, func_name)
        if key in seen:
            continue
        seen.add(key)
        wrapped = trace(
            limit=args.limit,
            storage_dir=storage_dir_abs,  # absolute path
            storage=storage,
        )(getattr(module, func_name))
        setattr(module, func_name, wrapped)
    logger.info(
        "Instrumentation applied to %d function(s): %s",
        len(seen),
        ", ".join(f"{m}.{f}" for (m, f) in seen),
    )

    # 6) Execute the target script
    script = cmd[0]
    if not script.endswith(".py"):
        logger.error("Unsupported script '%s': only .py files are allowed", script)
        sys.exit(1)

    sys.argv = cmd
    exit_code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        exit_code = getattr(exc, "code", 0)
        logger.info(
            "Target script exited with SystemExit(%s) — continuing to generation.",
            exit_code,
        )
    except KeyboardInterrupt:
        logger.warning(
            "Target script interrupted (KeyboardInterrupt) — continuing to generation."
        )
    except BaseException as exc:
        # Keep going: generate tests from whatever was traced
        logger.error("Target script terminated: %r — continuing to generation.", exc)

    # 7) Generate tests from ABSOLUTE calls dir
    out_file = getattr(args, "output", None)
    out_dir = getattr(args, "output_dir", None)
    if out_file is None and out_dir is None:
        out_file = Path("tests/test_pytead_generated.py")

    if out_dir is not None and not Path(out_dir).is_absolute():
        out_dir = caller_cwd / out_dir
    if out_file is not None and not Path(out_file).is_absolute():
        out_file = caller_cwd / out_file

    gen_formats = args.gen_formats or [args.format]
    logger.info(
        "TEAD: scanning traces in %s (formats=%s)...", calls_dir_abs, gen_formats
    )

    # Debug: how many files match?
    candidates = []
    for st in storages_from_names(gen_formats):
        candidates += list(calls_dir_abs.glob(f"*{st.extension}"))
    logger.info(
        "TEAD: found %d trace files matching %s.",
        len(candidates),
        [st.extension for st in storages_from_names(gen_formats)],
    )

    # Read entries
    try:
        entries = collect_entries(calls_dir=calls_dir_abs, formats=gen_formats)
    except Exception as exc:
        logger.error("Failed to collect entries: %r", exc)
        entries = {}

    if not entries:
        logger.warning(
            "TEAD: no traces found in '%s' for formats %s. Retrying with auto-detect.",
            calls_dir_abs,
            gen_formats,
        )
        entries = collect_entries(calls_dir=calls_dir_abs, formats=None)
        if not entries:
            logger.warning(
                "TEAD: still no traces after auto-detect — aborting generation."
            )
            return

    if args.only_targets:
        tgt = set(targets)
        entries = {k: v for (k, v) in entries.items() if k in tgt}
        if not entries:
            logger.warning(
                "TEAD: traces exist, but none match targeted functions: %s", sorted(tgt)
            )
            return

    total_unique = unique_count(entries)
    logger.info(
        "TEAD: found %d function(s), %d unique case(s).", len(entries), total_unique
    )

    if out_dir is not None:
        write_tests_per_func(entries, out_dir)
        logger.info("TEAD: generated %d test modules in '%s'.", len(entries), out_dir)
    else:
        source = render_tests(entries)
        write_tests(source, out_file)
        logger.info(
            "TEAD: generated '%s' with %d unique tests.", out_file, total_unique
        )


def add_tead_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "tead",
        help="trace functions by running a script, then immediately generate pytest tests",
    )

    # Instrumentation options (no hard-coded defaults here; let config fill them)
    p.add_argument(
        "-l",
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="max number of calls to record per function (default: 10)",
    )
    p.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory to store trace files (default: call_logs/)",
    )
    p.add_argument(
        "--format",
        choices=["pickle", "json", "repr"],
        default=argparse.SUPPRESS,
        help="trace storage format (default: pickle)",
    )
    p.add_argument(
        "-c",
        "--calls-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory to read traces for generation (default: --storage-dir)",
    )

    # Targets + script
    p.add_argument(
        "targets",
        nargs="*",
        default=argparse.SUPPRESS,
        metavar="target",
        help=(
            "one or more functions to trace, each in module.function format "
            "(may be provided via config [tead].targets)"
        ),
    )
    p.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- then the Python script to run (with arguments)",
    )

    # Pre-clean
    p.add_argument(
        "--pre-clean",
        action="store_true",
        default=argparse.SUPPRESS,
        help="before tracing, delete existing traces for the targeted functions in calls-dir/storage-dir",
    )
    p.add_argument(
        "--pre-clean-before",
        type=str,
        default=argparse.SUPPRESS,
        help="when pre-cleaning, only delete traces strictly older than this date/time (YYYY-MM-DD or ISO8601)",
    )

    # Generation
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "-o",
        "--output",
        type=Path,
        default=argparse.SUPPRESS,
        help="single-file output for generated tests (default if neither -o nor -d is given)",
    )
    group.add_argument(
        "-d",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="write one test module per function in this directory",
    )
    p.add_argument(
        "--gen-formats",
        choices=["pickle", "json", "repr"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict formats when reading traces for generation (default: the format just written)",
    )
    p.add_argument(
        "--only-targets",
        action="store_true",
        default=argparse.SUPPRESS,
        help="generate tests only for the functions traced in this command",
    )

    p.set_defaults(handler=run)
