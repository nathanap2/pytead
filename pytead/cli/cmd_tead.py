# pytead/cli/cmd_tead.py
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from typing import Optional, List

from .config_cli import (
    load_layered_config,
    apply_effective_to_args,
    effective_section,
)

from ._cli_utils import split_targets_and_cmd, fallback_targets_from_cfg, unique_count
from ._cli_utils import first_py_token as _first_py_token, resolve_under, require_script_py_or_exit

from ..logconf import configure_logger
from ..storage import get_storage
from ..imports import prepend_sys_path
from ..gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from ..targets import instrument_targets, resolve_target
from ..rt import resolve_attr
from ..tracing import trace as trace_decorator





def _project_root_from_config_source(src: Optional[Path]) -> Optional[Path]:
    if not src:
        return None
    cfg_dir = src.parent
    return cfg_dir.parent if cfg_dir.name == ".pytead" else cfg_dir


def _peek_wrapped_status(log, fqn: str) -> tuple[bool, str]:
    try:
        obj = resolve_attr(fqn)
    except Exception as exc:
        return False, f"resolve_attr({fqn!r}) failed: {exc!r}"
    base = getattr(obj, "__func__", obj)
    is_wrapped = hasattr(base, "__wrapped__")
    mod_name = fqn.rsplit(".", 1)[0]
    try:
        import importlib
        mod = importlib.import_module(mod_name)
        file_hint = getattr(mod, "__file__", None)
    except Exception:
        file_hint = None
    return is_wrapped, f"wrapped={is_wrapped} module={mod_name!r} file={file_hint!r} obj_id={id(base)}"


def run(args: argparse.Namespace) -> None:
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

    # --- 3) Cibles
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

    # --- 4) Racine & répertoire de traces
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

    # --- 5) Environnement d'import
    roots: List[str] = []
    if script_from_cmd:
        roots.append(str(script_from_cmd.parent.resolve()))
    if project_root:
        roots.append(str(project_root.resolve()))
    extra_paths = (
        getattr(args, "additional_sys_path", None)
        or (eff_tead or {}).get("additional_sys_path")
        or (eff_run or {}).get("additional_sys_path")
        or []
    )
    for p in extra_paths:
        pp = Path(p)
        roots.append(
            str((project_root / pp).resolve()) if not pp.is_absolute() else str(pp.resolve())
        )
    seen = set(); ordered = []
    for r in roots:
        if r not in seen:
            seen.add(r); ordered.append(r)
    prepend_sys_path(ordered)
    log.info("Import roots: %s", ordered)

    # --- 6) Instrumentation
    storage = get_storage(getattr(args, "format"))
    try:
        seen_targets = instrument_targets(
            targets, limit=getattr(args, "limit"), storage_dir=storage_dir_abs, storage=storage
        )
    except Exception as exc:
        for line in str(exc).splitlines():
            log.error(line)
        sys.exit(1)
    log.info(
        "Instrumentation applied to %d target(s): %s",
        len(seen_targets),
        ", ".join(sorted(seen_targets)),
    )

    # 🔎 Toujours logger le pré-check; rewrap défensif si besoin
    for fqn in sorted(seen_targets):
        ok, diag = _peek_wrapped_status(log, fqn)
        log.info("Pre-run check %s → %s", fqn, diag)
        if not ok:
            try:
                rt = resolve_target(fqn)
                installed = getattr(rt.owner, rt.attr)
                base = getattr(installed, "__func__", installed)
                if not hasattr(base, "__wrapped__"):
                    wrapped = trace_decorator(
                        limit=getattr(args, "limit"),
                        storage_dir=storage_dir_abs,
                        storage=storage,
                    )(base)
                    setattr(rt.owner, rt.attr, wrapped)
                    log.info("Defensive re-wrap applied to %s", fqn)
                ok2, diag2 = _peek_wrapped_status(log, fqn)
                log.info("Post-defensive check %s → %s", fqn, diag2)
            except Exception as exc:
                log.warning("Defensive re-wrap failed for %s: %r", fqn, exc)

    # --- 7) Exécution du script
    script_path = require_script_py_or_exit(cmd, log)
    script = str(script_path)

    sys.argv = cmd
    try:
        runpy.run_path(script, run_name="__main__")
        log.info("Script run completed: %s", script)
    except SystemExit as exc:
        log.info("Target script exited with SystemExit(%s) — continuing to generation.", getattr(exc, "code", 0))
    except KeyboardInterrupt:
        log.warning("Target script interrupted (KeyboardInterrupt) — continuing to generation.")
    except BaseException as exc:
        log.error("Target script terminated: %r — continuing to generation.", exc)

    # --- 8) Génération — chemins ancrés sur project_root
    out_file = getattr(args, "output", None)
    out_dir = getattr(args, "output_dir", None)
    if out_file is None and out_dir is None:
        out_file = project_root / "tests" / "test_pytead_generated.py"
    else:
        out_file = resolve_under(project_root, out_file)
        out_dir = resolve_under(project_root, out_dir)
        
    gen_formats = getattr(args, "gen_formats", None)
    only_targets = sorted(seen_targets) if getattr(args, "only_targets", False) else None
    import_roots = ordered  # déjà calculé plus haut

    from .service_cli import collect_and_emit_tests
    res = collect_and_emit_tests(
        storage_dir=storage_dir_abs,
        formats=gen_formats,
        output=out_file,
        output_dir=out_dir,
        import_roots=import_roots,
        only_targets=only_targets,
        logger=log,
    )
    if res:
        log.info("Generated %d unique case(s) across %d file(s).", res.unique_cases, res.files_written)
    else:
        log.warning("No tests generated (no traces or filter excluded everything).")


def add_tead_subparser(subparsers) -> None:
    p = subparsers.add_parser("tead", help="trace and immediately generate tests (all-in-one)")
    p.add_argument("-l", "--limit", type=int, default=argparse.SUPPRESS)
    p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--format", choices=["pickle", "graph-json"], default=argparse.SUPPRESS)
    p.add_argument("--gen-formats", choices=["pickle", "graph-json"], nargs="*", default=argparse.SUPPRESS)

    p.add_argument("--only-targets", action="store_true", default=argparse.SUPPRESS)
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("-o", "--output", type=Path, default=argparse.SUPPRESS)
    grp.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS)
    p.add_argument("--additional-sys-path", dest="additional_sys_path", nargs="*", default=argparse.SUPPRESS)
    p.add_argument("targets", nargs="*", default=argparse.SUPPRESS)
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(handler=run)

