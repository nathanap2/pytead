# pytead/cmd_gen.py
import argparse
import sys
from pathlib import Path
from typing import Dict, List

from .gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from .logconf import configure_logger
from ._cli_utils import unique_count
from .config import apply_config_from_default_file, LAST_CONFIG_PATH, get_effective_config


def _emptyish(x) -> bool:
    return x is None or (isinstance(x, (str, list, dict)) and len(x) == 0)


def _handle(args: argparse.Namespace) -> None:
    """Generate pytest tests from recorded traces."""
    logger = configure_logger(name="pytead.cli.gen")

    # 1) Layered config (packaged < user < project) → remplir sans écraser les flags CLI
    apply_config_from_default_file("gen", args)

    # Snapshots utiles pour diagnostiquer la stratification
    eff_gen = get_effective_config("gen")
    eff_run = get_effective_config("run")
    logger.info("GEN: effective config snapshot: %s", eff_gen or "{}")
    logger.info("GEN: project config path: %s", LAST_CONFIG_PATH or "<none>")

    # 2) Résolution robuste des chemins (nommage: storage_dir = répertoire des traces)
    storage_dir = getattr(args, "storage_dir", None)
    # Compat: accepter l'ancien nom --calls-dir (déprécié)
    calls_dir_compat = getattr(args, "calls_dir", None)
    if _emptyish(storage_dir) and not _emptyish(calls_dir_compat):
        storage_dir = calls_dir_compat
        logger.info("GEN: using --calls-dir as alias for --storage-dir (deprecated)")

    # Fallbacks:
    #   CLI --storage-dir  >
    #   [gen].storage_dir  >
    #   [run].storage_dir  >
    #   'call_logs'
    if _emptyish(storage_dir):
        storage_dir = (eff_gen or {}).get("storage_dir")
    if _emptyish(storage_dir):
        storage_dir = (eff_run or {}).get("storage_dir")
    if _emptyish(storage_dir):
        storage_dir = "call_logs"

    output = getattr(args, "output", None)
    output_dir = getattr(args, "output_dir", None)
    formats = getattr(args, "formats", None)

    # Sortie:
    #   CLI -o/-d >
    #   [gen].output_dir >
    #   'tests/test_pytead_generated.py'
    if _emptyish(output) and _emptyish(output_dir):
        output_dir = (eff_gen or {}).get("output_dir")
    if _emptyish(output) and _emptyish(output_dir):
        output = Path("tests/test_pytead_generated.py")

    logger.info(
        "GEN: resolved storage_dir=%r, output=%r, output_dir=%r (config=%s)",
        str(storage_dir) if storage_dir is not None else None,
        str(output) if output is not None else None,
        str(output_dir) if output_dir is not None else None,
        LAST_CONFIG_PATH or "<none>",
    )

    # 3) Validations (messages clairs et contextualisés)
    if _emptyish(storage_dir):
        logger.error(
            "GEN: unable to resolve 'storage_dir' after layering.\n"
            "Tried: CLI --storage-dir/--calls-dir, [gen].storage_dir, [run].storage_dir. "
            "Config used: %s ; effective [gen]=%s",
            LAST_CONFIG_PATH or "<none>",
            eff_gen or "{}",
        )
        sys.exit(1)

    storage_dir = Path(storage_dir)
    if not storage_dir.exists() or not storage_dir.is_dir():
        logger.error("Storage (calls) directory '%s' does not exist or is not a directory", storage_dir)
        sys.exit(1)

    if _emptyish(output) and _emptyish(output_dir):
        logger.error(
            "GEN: no output destination resolved. Provide -o/--output or -d/--output-dir, "
            "or set [gen].output_dir. Config used: %s ; effective [gen]=%s",
            LAST_CONFIG_PATH or "<none>",
            eff_gen or "{}",
        )
        sys.exit(1)

    # 4) Collecte + rendu
    entries = collect_entries(calls_dir=storage_dir, formats=formats)
    total_unique = unique_count(entries)

    # Prépare l’entête d’import dans les tests pour supporter des modules hors racine.
    gen_extra = getattr(args, "additional_sys_path", None) or []
    import_roots = ["."]
    import_roots += [str(p) for p in gen_extra]

    if not _emptyish(output_dir):
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

    # Nouveau nommage correct
    p_gen.add_argument(
        "-s",
        "--storage-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="directory containing trace files (defaults to [defaults]/[gen]/[run].storage_dir)",
    )

    # Compat (déprécié) : ex-flag --calls-dir
    p_gen.add_argument(
        "-c",
        "--calls-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="DEPRECATED alias for --storage-dir",
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

