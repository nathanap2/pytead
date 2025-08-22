import argparse
import sys
from pathlib import Path
from typing import Dict, List

from .gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from .logconf import configure_logger
from ._cli_utils import unique_count
from .config import apply_config_from_default_file, LAST_CONFIG_PATH


def _handle(args: argparse.Namespace) -> None:
    """Generate pytest tests from recorded traces."""
    logger = configure_logger(name="pytead.cli.gen")

    # Remplissage via config (packagé < user < local)
    apply_config_from_default_file("gen", args)

    # Options requises (pas de defaults hard-codés ici)
    calls_dir = getattr(args, "calls_dir", None)
    output = getattr(args, "output", None)
    output_dir = getattr(args, "output_dir", None)
    formats = getattr(args, "formats", None)

    if calls_dir is None:
        logger.error(
            "Missing 'calls_dir' for 'gen'. Please set [gen].calls_dir in .pytead/config or pass -c/--calls-dir."
        )
        sys.exit(1)
    if output is None and output_dir is None:
        logger.error(
            "You must set either [gen].output or [gen].output_dir in .pytead/config or pass -o/--output or -d/--output-dir."
        )
        sys.exit(1)

    calls_dir = Path(calls_dir)
    if not calls_dir.exists() or not calls_dir.is_dir():
        logger.error("Calls directory '%s' does not exist or is not a directory", calls_dir)
        sys.exit(1)

    entries = collect_entries(calls_dir=calls_dir, formats=formats)
    total_unique = unique_count(entries)

    # Prépare l’entête d’import dans les tests pour supporter des modules hors racine.
    # On utilise des chemins *relatifs à la racine du projet* et résolus à l’exécution.
    gen_extra = getattr(args, "additional_sys_path", None) or []
    import_roots = ["."]
    import_roots += [str(p) for p in gen_extra]

    if output_dir is not None:
        write_tests_per_func(entries, Path(output_dir), import_roots=import_roots)
        logger.info(
            "Generated %d test modules in '%s' (%d total unique tests)",
            len(entries),
            output_dir,
            total_unique,
        )
    else:
        source = render_tests(entries, import_roots=import_roots)
        write_tests(source, Path(output))
        logger.info("Generated '%s' with %d unique tests", output, total_unique)


def add_gen_subparser(subparsers) -> None:
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
        help="restrict to these formats when reading",
    )
    # NB: pas de flag CLI pour additional_sys_path ; passez-le via la config:
    # [gen].additional_sys_path = ["src", "third_party/pkg", ...]
    p_gen.set_defaults(handler=_handle)

