import argparse
import importlib
import logging
import runpy
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple

from pytead.tracing import trace
from pytead.gen_tests import (
    collect_entries,
    render_tests,
    write_tests,
    write_tests_per_func,
)
from pytead.storage import get_storage
from pytead._cases import unique_cases


def configure_logger(level: int = logging.INFO) -> logging.Logger:
    """
    Configure a single StreamHandler for the 'pytead' logger (CLI context only).
    The library itself stays quiet (it uses a NullHandler).
    """
    root = logging.getLogger("pytead")
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[pytead] %(levelname)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger("pytead.cli")


def _unique_count(entries_by_func: Dict[str, List[Dict[str, Any]]]) -> int:
    return sum(len(unique_cases(entries)) for entries in entries_by_func.values())


def _split_targets_and_cmd(args: argparse.Namespace) -> Tuple[List[str], List[str]]:
    """
    Make parsing resilient:
    - If '--' ended up in targets, split there.
    - Always strip a leading '--' from cmd.
    - If someone forgot '--' and put a *.py directly after targets,
      split at the first token ending with '.py'.
    """
    targets = list(args.targets or [])
    cmd = list(args.cmd or [])

    # 1) If '--' accidentally captured into targets, split
    if "--" in targets:
        sep = targets.index("--")
        cmd = targets[sep + 1 :] + cmd
        targets = targets[:sep]

    # 2) Normalize 'cmd': drop leading '--' if present
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    # 3) If a .py sneaked into targets (forgotten '--'), split there
    for i, tok in enumerate(targets):
        if tok.endswith(".py"):
            cmd = targets[i:] + cmd
            targets = targets[:i]
            break

    return targets, cmd


def _cmd_run(args: argparse.Namespace) -> None:
    """Instrument one or more functions and run the target script."""
    logger = configure_logger()

    targets, cmd = _split_targets_and_cmd(args)

    if not targets:
        logger.error("No target provided. Expect at least one 'module.function'.")
        sys.exit(1)
    if not cmd:
        logger.error("No script specified after '--'")
        sys.exit(1)

    sys.path.insert(0, str(Path.cwd()))

    # Select storage backend (pickle by default)
    storage = get_storage(args.format)

    # --- Phase 1: resolve & validate all targets ---
    resolved = []  # list[(module, module_name, func_name)]
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

    # --- Phase 2: instrument all targets ---
    seen = set()
    for module, module_name, func_name in resolved:
        key = (module_name, func_name)
        if key in seen:
            continue
        seen.add(key)
        wrapped = trace(
            limit=args.limit,
            storage_dir=args.storage_dir,
            storage=storage,
        )(getattr(module, func_name))
        setattr(module, func_name, wrapped)
    logger.info(
        "Instrumentation applied to %d function(s): %s",
        len(seen),
        ", ".join(f"{m}.{f}" for (m, f) in seen),
    )

    # Execute the script
    script = cmd[0]
    if not script.endswith(".py"):
        logger.error("Unsupported script '%s': only .py files are allowed", script)
        sys.exit(1)

    sys.argv = cmd
    try:
        runpy.run_path(script, run_name="__main__")
    except Exception as exc:
        logger.error("Error during script execution: %s", exc)
        sys.exit(1)


def _cmd_gen(args: argparse.Namespace) -> None:
    """Generate pytest tests from recorded traces."""
    logger = configure_logger()

    # Default to single output file if neither -o nor -d were given
    if args.output is None and args.output_dir is None:
        args.output = Path("tests/test_pytead_generated.py")

    if not args.calls_dir.exists() or not args.calls_dir.is_dir():
        logger.error(
            "Calls directory '%s' does not exist or is not a directory", args.calls_dir
        )
        sys.exit(1)

    entries = collect_entries(calls_dir=args.calls_dir, formats=args.formats)
    total_unique = _unique_count(entries)

    if args.output_dir is not None:
        write_tests_per_func(entries, args.output_dir)
        logger.info(
            "Generated %d test modules in '%s' (%d total unique tests)",
            len(entries),
            args.output_dir,
            total_unique,
        )
    else:
        source = render_tests(entries)
        write_tests(source, args.output)
        logger.info("Generated '%s' with %d unique tests", args.output, total_unique)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pytead")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `run` subcommand
    p_run = subparsers.add_parser(
        "run",
        help="instrument one or more functions and execute a Python script (use -- to separate targets from the script)",
    )
    p_run.add_argument(
        "-l",
        "--limit",
        type=int,
        default=10,
        help="max number of calls to record per function (default: 10)",
    )
    p_run.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=Path("call_logs"),
        help="directory to store trace files (default: call_logs/)",
    )
    p_run.add_argument(
        "--format",
        choices=["pickle", "json", "repr"],
        default="pickle",
        help="trace storage format (default: pickle)",
    )
    p_run.add_argument(
        "targets",
        nargs="+",
        metavar="target",
        help="one or more functions to trace, each in module.function format",
    )
    p_run.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- then the Python script to run (with arguments)",
    )
    p_run.set_defaults(func=_cmd_run)

    # `gen` subcommand
    p_gen = subparsers.add_parser("gen", help="generate pytest tests from traces")
    p_gen.add_argument(
        "-c",
        "--calls-dir",
        type=Path,
        default=Path("call_logs"),
        help="directory containing trace files (default: call_logs/)",
    )
    group = p_gen.add_mutually_exclusive_group()
    group.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="single-file output for generated tests",
    )
    group.add_argument(
        "-d",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=None,
        help="write one test module per function in this directory",
    )
    p_gen.add_argument(
        "--formats",
        choices=["pickle", "json", "repr"],
        nargs="*",
        help="restrict to these formats when reading (default: auto-detect all)",
    )
    p_gen.set_defaults(func=_cmd_gen)

    # Future subcommands: clean, doc, etc.

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
