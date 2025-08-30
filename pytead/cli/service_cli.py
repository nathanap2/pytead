# pytead/cli/service_cli.py
from __future__ import annotations

import logging
import runpy
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..imports import compute_import_roots, prepend_sys_path
from ..storage import get_storage as _get_storage
from ..gen_tests import collect_entries, render_tests, write_tests, write_tests_per_func
from ._cli_utils import unique_count  # optionally swap to a with-self variant
from ..typing_defs import StorageLike


__all__ = [
    "RunStatus",
    "RunOutcome",
    "InstrumentResult",
    "GenerationResult",
    "prepare_import_env",
    "instrument_targets",
    "run_script",
    "collect_traces",
    "emit_tests",
    "instrument_and_run",
    "collect_and_emit_tests",
]


# ---------- Result models ----------

class RunStatus(Enum):
    OK = auto()
    SYSTEM_EXIT = auto()
    KEYBOARD_INTERRUPT = auto()
    EXCEPTION = auto()


@dataclass
class RunOutcome:
    """Outcome of running a Python script with runpy."""
    status: RunStatus
    detail: Optional[str] = None
    exit_code: Optional[int] = None


@dataclass
class InstrumentResult:
    """Summary of target instrumentation."""
    seen: frozenset[str]     # fully-qualified names that were instrumented
    storage_dir: Path
    format_name: str         # "pickle" | "json" | "repr"


@dataclass
class GenerationResult:
    """Summary of test generation."""
    files_written: int
    unique_cases: int
    output: Optional[Path] = None
    output_dir: Optional[Path] = None


# ---------- Low-level services (no argparse, no config IO) ----------

def prepare_import_env(
     script_path: Optional[Path],
     additional_paths: Iterable[Path] = (),
     *,
     project_root: Optional[Path] = None,
     logger: Optional[logging.Logger] = None,
 ) -> List[str]:
    """
    Compute and prepend import roots into sys.path:
      1) script directory (if provided),
      2) project root (derived from config.LAST_CONFIG_PATH),
      3) any additional paths.
    Returns the effective absolute roots inserted.
    """
    roots = compute_import_roots(script_path, additional_paths, project_root=project_root)
    prepend_sys_path(roots)
    if logger:
        logger.info("Import roots: %s", roots)
    return roots


def instrument_targets(
    targets: Sequence[str],
    limit: int,
    storage_dir: Path,
    storage: StorageLike | str = "pickle",
    *,
    logger: Optional[logging.Logger] = None,
) -> InstrumentResult:
    """
    Resolve and instrument targets. `storage` may be a StorageLike or a name ("pickle"/"json"/"repr").
    """
    
    from ..targets import instrument_targets as targets_instrument

    st = _get_storage(storage) if isinstance(storage, str) else storage
    seen = targets_instrument(targets, limit=limit, storage_dir=storage_dir, storage=st)
    if logger:
        logger.info("Instrumented %d target(s): %s", len(seen), ", ".join(sorted(seen)))
    return InstrumentResult(seen=frozenset(seen), storage_dir=storage_dir, format_name=st.extension.lstrip("."))



def run_script(
    script_file: Path,
    argv: List[str],
    *,
    logger: Optional[logging.Logger] = None,
) -> RunOutcome:
    """
    Execute a .py file with runpy, setting sys.argv to `argv`. Does not exit the caller.
    """
    if script_file.suffix != ".py":
        return RunOutcome(RunStatus.EXCEPTION, detail=f"Unsupported script: {script_file}")

    sys.argv = argv
    try:
        runpy.run_path(str(script_file), run_name="__main__")
        return RunOutcome(RunStatus.OK)
    except SystemExit as exc:
        code = getattr(exc, "code", 0)
        if logger:
            logger.info("Script exited with SystemExit(%s).", code)
        return RunOutcome(RunStatus.SYSTEM_EXIT, exit_code=code)
    except KeyboardInterrupt:
        if logger:
            logger.warning("Script interrupted (KeyboardInterrupt).")
        return RunOutcome(RunStatus.KEYBOARD_INTERRUPT)
    except BaseException as exc:
        if logger:
            logger.error("Script terminated: %r", exc)
        return RunOutcome(RunStatus.EXCEPTION, detail=repr(exc))


def collect_traces(
    storage_dir: Path,
    formats: Optional[List[str]] = None,
    *,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Load all trace entries grouped by function FQN from `storage_dir`.
    """
    entries = collect_entries(storage_dir=storage_dir, formats=formats)
    if logger:
        logger.info("Collected traces for %d function(s).", len(entries))
    return entries


def emit_tests(
    entries_by_func: Dict[str, List[Dict[str, Any]]],
    *,
    output_dir: Path,
    import_roots: Optional[List[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> GenerationResult:
    """
    Write one test module per function into `output_dir`.

    Parameters
    ----------
    entries_by_func : Dict[str, List[Dict[str, Any]]]
        Collected trace entries grouped by fully-qualified function name.
    output_dir : Path
        Destination directory where one test file per function will be written.
    import_roots : Optional[List[str]]
        Absolute import roots to embed into generated test files so that user
        modules can be imported reliably in the test environment.
    logger : Optional[logging.Logger]
        Optional logger for progress messages.

    Returns
    -------
    GenerationResult
        Summary with number of files written, unique cases count, and the output directory.
    """
    # Count unique cases across all functions (graph-json and pickle handled upstream).
    uniq = unique_count(entries_by_func)

    # Write one file per function. The writer creates the directory if needed.
    write_tests_per_func(entries_by_func, output_dir, import_roots=import_roots)

    if logger:
        logger.info("Wrote %d test module(s) into '%s'.", len(entries_by_func), output_dir)

    return GenerationResult(
        files_written=len(entries_by_func),
        unique_cases=uniq,
        output_dir=output_dir,
    )



# ---------- Composed services used by commands ----------

def instrument_and_run(
    *,
    targets: Sequence[str],
    limit: int,
    storage_dir: Path,
    storage: StorageLike | str,
    script_file: Path,
    argv: List[str],
    additional_sys_path: Iterable[Path] = (),
    logger: Optional[logging.Logger] = None,
) -> Tuple[InstrumentResult, RunOutcome, List[str]]:
    """
    1) Prepare sys.path, 2) instrument targets, 3) run the script.
    Returns (instrumentation result, run outcome, effective import roots).
    """
    roots = prepare_import_env(script_file, additional_sys_path, logger=logger)
    instr = instrument_targets(targets, limit, storage_dir, storage, logger=logger)
    outcome = run_script(script_file, argv, logger=logger)
    return instr, outcome, roots


def collect_and_emit_tests(
    *,
    storage_dir: Path,
    formats: Optional[List[str]],
    output_dir: Optional[Path],
    import_roots: Optional[List[str]] = None,
    only_targets: Optional[Iterable[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[GenerationResult]:
    """
    1) Collect traces from `storage_dir`,
    2) optionally filter by `only_targets`,
    3) emit tests one file per function into `output_dir`.

    Returns
    -------
    Optional[GenerationResult]
        A summary if something was written; None if nothing to do (no traces, or
        filtered out), or if the storage directory does not exist.
    """
    # Precondition: storage directory must exist.
    if not storage_dir.exists():
        if logger:
            logger.info("Storage dir '%s' does not exist — skipping generation.", storage_dir)
        return None

    # Load traces (grouped by fully-qualified function name).
    entries = collect_traces(storage_dir, formats, logger=logger)
    if not entries:
        if logger:
            logger.warning("No traces found in '%s'.", storage_dir)
        return None

    # Optional filtering by a subset of targets.
    if only_targets:
        tgt = set(only_targets)
        entries = {k: v for (k, v) in entries.items() if k in tgt}
        if not entries:
            if logger:
                logger.warning("Traces exist but none match targets: %s", sorted(tgt))
            return None

    # No implicit defaults here: the caller must provide output_dir.
    if output_dir is None:
        raise ValueError("collect_and_emit_tests: 'output_dir' must be provided by the caller (see config).")

    # Emit one test file per function.
    return emit_tests(
        entries_by_func=entries,
        output_dir=output_dir,
        import_roots=import_roots,
        logger=logger,
    )


