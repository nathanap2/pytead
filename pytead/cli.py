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
from pytead.logconf import configure_logger

from pytead.clean import add_subparser as add_clean_subparser
from pytead.tead_all_in_one import add_subparser as add_tead_subparser

# Config: read-only from default_config.toml (project root or parents)
from .config import (
    apply_config_from_default_file,
    get_effective_config,
    LAST_CONFIG_PATH,
)


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

    if "--" in targets:
        sep = targets.index("--")
        cmd = targets[sep + 1 :] + cmd
        targets = targets[:sep]

    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    for i, tok in enumerate(targets):
        if tok.endswith(".py"):
            cmd = targets[i:] + cmd
            targets = targets[:i]
            break

    return targets, cmd


def _cmd_run(args: argparse.Namespace) -> None:
    """Instrument one or more functions and run the target script."""
    logger = configure_logger(name="pytead.cli")

    # 0) Show raw args as parsed by argparse (before config injection)
    logger.info(
        "RUN: raw args (pre-config): %s", {k: getattr(args, k) for k in vars(args)}
    )

    # 1) Fill from default_config.toml (does NOT override explicit CLI flags)
    apply_config_from_default_file("run", args)

    # 2) Show effective args after config injection
    logger.info(
        "RUN: effective args (post-config): %s",
        {k: getattr(args, k) for k in vars(args)},
    )

    # 3) Validate required options (no in-code defaults here)
    missing = [k for k in ("limit", "storage_dir", "format") if not hasattr(args, k)]
    if missing:
        logger.error(
            "Missing required options for 'run': %s. "
            "Please set them in [defaults] or [run] of default_config.toml or pass flags.",
            ", ".join(missing),
        )
        sys.exit(1)

    # Snapshot effective config for potential fallback of targets
    effective_cfg = get_effective_config("run")
    logger.info("RUN: effective config snapshot: %s", effective_cfg or "{}")

    # 4) Split positional targets and the '-- script.py [args...]' part
    targets, cmd = _split_targets_and_cmd(args)
    logger.info("RUN: split targets=%s cmd=%s", targets, cmd)

    # Fallback: if targets ended up empty (e.g., because a *.py was moved to cmd),
    # let config supply the function targets.
    if not targets:
        cfg_targets = (
            effective_cfg.get("targets") if isinstance(effective_cfg, dict) else None
        )
        if cfg_targets:
            logger.info(
                "RUN: no CLI targets after split; falling back to config targets: %s",
                cfg_targets,
            )
            targets = list(cfg_targets)

    if not targets:
        logger.error(
            "No target provided. Expect at least one 'module.function'. Config file used: %s",
            LAST_CONFIG_PATH,
        )
        sys.exit(1)
    if not cmd:
        logger.error("No script specified after '--'")
        sys.exit(1)

    # 5) Prepare import path and storage backend
    sys.path.insert(0, str(Path.cwd()))
    storage = get_storage(args.format)

    # 6) Resolve module.function targets and apply instrumentation
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

    # 7) Execute the target script
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
    logger = configure_logger(name="pytead.cli")

    # Fill from default_config.toml
    apply_config_from_default_file("gen", args)

    # Validate required options (no in-code defaults)
    calls_dir = getattr(args, "calls_dir", None)
    output = getattr(args, "output", None)
    output_dir = getattr(args, "output_dir", None)
    formats = getattr(args, "formats", None)

    if calls_dir is None:
        logger.error(
            "Missing 'calls_dir' for 'gen'. Please set [gen].calls_dir in default_config.toml "
            "or pass -c/--calls-dir."
        )
        sys.exit(1)
    if output is None and output_dir is None:
        logger.error(
            "You must set either [gen].output or [gen].output_dir in default_config.toml "
            "or pass -o/--output or -d/--output-dir."
        )
        sys.exit(1)

    calls_dir = Path(calls_dir)
    if not calls_dir.exists() or not calls_dir.is_dir():
        logger.error(
            "Calls directory '%s' does not exist or is not a directory", calls_dir
        )
        sys.exit(1)

    entries = collect_entries(calls_dir=calls_dir, formats=formats)
    total_unique = _unique_count(entries)

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="pytead")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `run`
    p_run = subparsers.add_parser(
        "run",
        help="instrument one or more functions and execute a Python script (use -- to separate targets from the script)",
    )
    # No hard-coded defaults: config file fills missing values
    p_run.add_argument(
        "-l",
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="max number of calls to record per function",
    )
    p_run.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory to store trace files",
    )
    p_run.add_argument(
        "--format",
        choices=["pickle", "json", "repr"],
        default=argparse.SUPPRESS,
        help="trace storage format",
    )
    p_run.add_argument(
        "targets",
        nargs="*",
        default=argparse.SUPPRESS,
        metavar="target",
        help="one or more functions to trace, each in module.function format "
        "(may be provided via config [run].targets)",
    )

    p_run.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- then the Python script to run (with arguments)",
    )
    p_run.set_defaults(
        handler=_cmd_run
    )  # keep 'handler' to avoid collision with --func

    # `gen`
    p_gen = subparsers.add_parser("gen", help="generate pytest tests from traces")
    p_gen.add_argument(
        "-c",
        "--calls-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing trace files",
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
        help="restrict to these formats when reading (default: auto-detect all)",
    )
    p_gen.set_defaults(handler=_cmd_gen)

    add_clean_subparser(subparsers)
    add_tead_subparser(subparsers)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
