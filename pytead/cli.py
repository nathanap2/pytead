import argparse
import importlib
import logging
import runpy
import sys
from pathlib import Path
from typing import List

from pytead.decorator import pytead
from pytead.gen_tests import (
    collect_entries,
    render_tests,
    write_tests,
    write_tests_per_func,
)


def configure_logger() -> None:
    logger = logging.getLogger(__name__)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[pytead] %(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _cmd_run(args: argparse.Namespace) -> None:
    """Instrument a function and run the target script."""
    configure_logger()
    logger = logging.getLogger(__name__)

    try:
        module_name, func_name = args.target.rsplit(".", 1)
    except ValueError:
        logger.error("Invalid target '%s': expected format module.function", args.target)
        sys.exit(1)

    sys.path.insert(0, str(Path.cwd()))
    module = importlib.import_module(module_name)
    if not hasattr(module, func_name):
        logger.error("Function '%s' not found in module '%s'", func_name, module_name)
        sys.exit(1)

    wrapped = pytead(
        limit=args.limit,
        storage_dir=args.storage_dir
    )(getattr(module, func_name))
    setattr(module, func_name, wrapped)
    logger.info("Instrumentation applied to %s.%s", module_name, func_name)

    # Execute the script
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        logger.error("No script specified after '--'")
        sys.exit(1)

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
    configure_logger()
    logger = logging.getLogger(__name__)

    if not args.calls_dir.exists() or not args.calls_dir.is_dir():
        logger.error("Calls directory '%s' does not exist or is not a directory", args.calls_dir)
        sys.exit(1)

    entries = collect_entries(calls_dir=args.calls_dir)

    if args.output_dir is not None:
        # generate one test module per function
        write_tests_per_func(entries, args.output_dir)
        logger.info(
            "Generated %d test modules in '%s'",
            len(entries),
            args.output_dir
        )
    else:
        # generate a single test file
        source = render_tests(entries)
        write_tests(source, args.output)
        total = sum(len(calls) for calls in entries.values())
        logger.info("Generated '%s' with %d tests", args.output, total)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pytead")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `run` subcommand
    p_run = subparsers.add_parser(
        "run",
        help="instrument a function and execute a Python script"
    )
    p_run.add_argument(
        "-l", "--limit", type=int, default=10,
        help="max number of calls to record per function (default: 10)"
    )
    p_run.add_argument(
        "-s", "--storage-dir", type=Path, default=Path("call_logs"),
        help="directory to store trace files (default: call_logs/)"
    )
    p_run.add_argument(
        "target",
        help="target function to trace, in module.function format"
    )
    p_run.add_argument(
        "cmd", nargs=argparse.REMAINDER,
        help="-- then the Python script to run (with arguments)"
    )
    p_run.set_defaults(func=_cmd_run)

    # `gen` subcommand
    p_gen = subparsers.add_parser(
        "gen",
        help="generate pytest tests from trace pickles"
    )
    p_gen.add_argument(
        "-c", "--calls-dir", type=Path, default=Path("call_logs"),
        help="directory containing .pkl trace files (default: call_logs/)"
    )
    p_gen.add_argument(
        "-o", "--output", type=Path,
        default=Path("tests/test_pytead_generated.py"),
        help="single-file output for generated tests (default: %(default)s)"
    )
    p_gen.add_argument(
        "-d", "--output-dir", dest="output_dir", type=Path,
        default=None,
        help="if set, write one test module per function in this directory"
    )
    p_gen.set_defaults(func=_cmd_gen)

    # Future subcommands: clean, doc, etc.

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

