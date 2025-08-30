# pytead/cli/cmd_tead.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, List

from .config_cli import load_layered_config, apply_effective_to_args, effective_section
from ._cli_utils import (
    split_targets_and_cmd,
    fallback_targets_from_cfg,
)
from ._cli_utils import (
    first_py_token as _first_py_token,
    resolve_under,
    require_script_py_or_exit,
)

from ..logconf import configure_logger
from . import service_cli as svc


def _project_root_from_config_source(src: Optional[Path]) -> Optional[Path]:
    """
    Si la config projet provient de '<root>/.pytead/config.*', remonte d'un cran
    pour obtenir la racine projet; sinon retourne le dossier de la config.
    """
    if not src:
        return None
    cfg_dir = src.parent
    return cfg_dir.parent if cfg_dir.name == ".pytead" else cfg_dir


def run(args: argparse.Namespace) -> None:
    """
    Implémentation de `pytead tead` :
      - charge la config en couches et applique les valeurs effectives,
      - résout cibles + script,
      - délègue instrumentation + exécution à la couche services,
      - génère les tests depuis les traces collectées.

    La logique est volontairement mince; toute la “vraie” mécanique vit dans
    pytead/cli/service_cli.py.
    """
    log = configure_logger(name="pytead.tead")

    # --- 0) Positionnels
    raw_targets = getattr(args, "targets", []) or []
    raw_cmd = getattr(args, "cmd", []) or []
    targets, cmd = split_targets_and_cmd(raw_targets, raw_cmd)

    # Indice de départ pour la découverte de config
    script_from_cmd = _first_py_token(cmd)
    script_from_targets = _first_py_token(targets)
    start_hint: Optional[Path] = (
        script_from_cmd.parent
        if script_from_cmd
        else (script_from_targets.parent if script_from_targets else None)
    )

    # --- 1) Config en couches & remplissage
    ctx = load_layered_config(start=start_hint)
    apply_effective_to_args("tead", ctx, args)

    # --- 2) Exigences minimales
    missing = [k for k in ("limit", "storage_dir", "format") if not hasattr(args, k)]
    if missing:
        log.error(
            "Missing required options for 'tead': %s. Provide them via CLI or config "
            "([defaults]/[tead]). Config used: %s",
            ", ".join(missing),
            str(ctx.source_path) if ctx.source_path else "<none>",
        )
        sys.exit(1)

    # --- 3) Cibles (CLI → fallback config [tead] puis [run])
    eff_tead = effective_section(ctx, "tead")
    eff_run = effective_section(ctx, "run")

    targets = fallback_targets_from_cfg(targets, eff_tead, log, "TEAD")
    if not targets:
        run_targets = (eff_run or {}).get("targets")
        if run_targets:
            targets = list(run_targets)
    if not targets:
        log.error("No target provided. Config used: %s", ctx.source_path or "<none>")
        sys.exit(1)
    if not cmd:
        log.error("No script specified after '--'")
        sys.exit(1)

    # --- 4) Racine & répertoire de traces (ancrés)
    project_root = (
        _project_root_from_config_source(ctx.source_path)
        or (script_from_cmd.parent if script_from_cmd else None)
        or Path.cwd()
    )
    raw_storage = Path(getattr(args, "storage_dir"))
    storage_dir_abs = (
        raw_storage if raw_storage.is_absolute() else (project_root / raw_storage)
    ).resolve()
    storage_dir_abs.mkdir(parents=True, exist_ok=True)

    log.info("Project root: %s", project_root)
    log.info("Storage dir : %s", storage_dir_abs)

    # --- 5) Chemins d'import supplémentaires (relatifs ancrés sur project_root)
    extra_paths = (
        getattr(args, "additional_sys_path", None)
        or (eff_tead or {}).get("additional_sys_path")
        or (eff_run or {}).get("additional_sys_path")
        or []
    )
    add_paths: List[Path] = []
    for p in extra_paths:
        pp = Path(p)
        add_paths.append(pp if pp.is_absolute() else (project_root / pp))

    # --- 6) Instrumentation + exécution via service layer
    script_path = require_script_py_or_exit(cmd, log)
    try:
        instr, outcome, roots = svc.instrument_and_run(
            targets=targets,
            limit=getattr(args, "limit"),
            storage_dir=storage_dir_abs,
            storage=getattr(args, "format"),
            script_file=script_path,
            argv=cmd,
            additional_sys_path=add_paths,
            logger=log,
        )
    except Exception as exc:
        log.error("TEAD failed during instrument+run: %s", exc)
        sys.exit(1)

    log.info(
        "Instrumentation applied to %d target(s): %s",
        len(instr.seen),
        ", ".join(sorted(instr.seen)),
    )
    if outcome.status.name == "OK":
        log.info("Script run completed successfully.")
    elif outcome.status.name == "SYSTEM_EXIT":
        log.info("Script exited with code %s.", outcome.exit_code)
    elif outcome.status.name == "KEYBOARD_INTERRUPT":
        log.warning("Script interrupted (KeyboardInterrupt).")
    else:
        log.error("Script terminated abnormally: %s", outcome.detail or "unknown error")

    # --- 7) Génération — chemins ancrés sur project_root
    out_dir = args.output_dir
    out_dir = resolve_under(project_root, out_dir)
        

    gen_formats = getattr(args, "gen_formats", None)
    only_targets = sorted(instr.seen) if getattr(args, "only_targets", False) else None
    res = svc.collect_and_emit_tests(
        storage_dir=storage_dir_abs,
        formats=gen_formats,
        output_dir=out_dir,
        import_roots=roots,  # racines réellement utilisées pendant l'exécution
        only_targets=only_targets,
        logger=log,
    )
    if res:
        log.info(
            "Generated %d unique case(s) across %d file(s).",
            res.unique_cases,
            res.files_written,
        )
    else:
        log.warning("No tests generated (no traces or filter excluded everything).")


def add_tead_subparser(subparsers) -> None:
    p = subparsers.add_parser("tead", help="trace and immediately generate tests (all-in-one)")
    p.add_argument("-l", "--limit", type=int, default=argparse.SUPPRESS)
    p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--format", choices=["pickle", "graph-json"], default=argparse.SUPPRESS)
    p.add_argument("--gen-formats", choices=["pickle", "graph-json"], nargs="*", default=argparse.SUPPRESS)

    p.add_argument("--only-targets", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--additional-sys-path", dest="additional_sys_path", nargs="*", default=argparse.SUPPRESS)
    p.add_argument("targets", nargs="*", default=argparse.SUPPRESS)
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(handler=run)

