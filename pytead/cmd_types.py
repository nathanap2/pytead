# pytead/cmd_types.py
import argparse
import sys
from pathlib import Path
from typing import Dict, List

from .logconf import configure_logger
from .config import apply_config_from_default_file
from .gen_tests import collect_entries
from ._cases import unique_cases
from .gen_types import summarize_function_types, group_by_module, render_stub_module


def _handle(args: argparse.Namespace) -> None:
    log = configure_logger(name="pytead.cli.types")
    apply_config_from_default_file("types", args)

    # --- NEW: rendre le repo importable ---
    import sys
    from pathlib import Path

    project_root = Path(getattr(args, "project_root", Path.cwd()))
    sys.path.insert(0, str(project_root.resolve()))
    # --------------------------------------

    calls_dir = getattr(args, "calls_dir", None)
    out_dir = getattr(args, "out_dir", None)
    formats = getattr(args, "formats", None)

    if calls_dir is None or out_dir is None:
        log.error(
            "You must provide both --calls-dir and --out-dir (or set them in [types])."
        )
        sys.exit(1)

    calls_dir = Path(calls_dir)
    out_dir = Path(out_dir)

    if not calls_dir.exists() or not calls_dir.is_dir():
        log.error(
            "Calls directory '%s' does not exist or is not a directory", calls_dir
        )
        sys.exit(1)

    entries_by_func: Dict[str, List[dict]] = collect_entries(
        calls_dir=calls_dir, formats=formats
    )
    if not entries_by_func:
        log.error("No traces found in '%s' (formats=%s).", calls_dir, formats or "auto")
        sys.exit(1)

    typed_infos: Dict[str, any] = {}
    for func, entries in entries_by_func.items():
        samples = [
            {"args": a, "kwargs": k, "result": r} for (a, k, r) in unique_cases(entries)
        ]
        if not samples:
            continue
        try:
            info = summarize_function_types(
                func, samples
            )  # fallback intégré si import impossible
            typed_infos[func] = info
        except Exception as exc:
            log.warning("Type inference failed for %s: %s", func, exc)

    if not typed_infos:
        log.error("No usable samples for type inference.")
        sys.exit(1)

    grouped = group_by_module(typed_infos)
    written = 0
    for module, funcs in grouped.items():
        rel = Path(*module.split("."))
        dest = out_dir / rel.with_suffix(".pyi")
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = render_stub_module(module, funcs)
        dest.write_text(src, encoding="utf-8")
        written += 1
        log.info("Wrote stub for module %s → %s", module, dest)

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
        help="directory containing trace files (defaults to [types].calls_dir)",
    )
    p.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="output directory root for .pyi files (defaults to [types].out_dir)",
    )
    p.add_argument(
        "--formats",
        choices=["pickle", "json", "repr"],
        nargs="*",
        default=argparse.SUPPRESS,
        help="restrict formats when reading traces",
    )
    p.add_argument(
        "--project-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="path added to sys.path for imports (default: CWD)",
    )
    p.set_defaults(handler=_handle)
