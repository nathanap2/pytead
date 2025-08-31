# pytead/cli/_common.py
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Dict, Any

from ..logconf import configure_logger
from .config_cli import effective_section
from ._cli_utils import (
    load_ctx_and_fill,
    ensure_storage_dir_or_exit,
    resolve_storage_dir_for_write,
    require_output_dir_or_exit,
    normalize_additional_sys_path,
    fallback_targets_from_cfg,
)

FORMAT_CHOICES = ("pickle", "graph-json")

def make_logger(cmd: str):
    return configure_logger(name=f"pytead.cli.{cmd}")


def load_ctx_anchored(section: str, args: argparse.Namespace, anchor: Path | str | None):
    return load_ctx_and_fill(section, args, lambda _a: anchor)

def eff(ctx, section: str) -> Dict[str, Any]:
    return effective_section(ctx, section) or {}

def norm_roots(project_root: Path, add_sys: Optional[Iterable[str] | Iterable[Path]]):
    abs_strs = normalize_additional_sys_path(project_root, add_sys)
    path_list = [Path(p) for p in (abs_strs or [])]
    return abs_strs or None, path_list


def storage_for_read(ctx, arg_value, log, section: str) -> Path:
    return ensure_storage_dir_or_exit(ctx, section, arg_value, log)

def storage_for_write(ctx, arg_value, log) -> Path:
    return resolve_storage_dir_for_write(ctx, arg_value, log)

def pick_output_dir(ctx, arg_value, sections: Sequence[Dict[str, Any]], log, *, section: str) -> Path:
    """
    Minimal policy: prefer the explicit arg if given, else the first section that specifies output_dir/out_dir.
    """
    if arg_value is not None:
        return require_output_dir_or_exit(ctx, arg_value, log, section=section)
    for s in sections:
        v = s.get("output_dir") or s.get("out_dir")
        if v:
            return require_output_dir_or_exit(ctx, v, log, section=section)
    # If nothing found, pass through to require_output_dir_or_exit (will behave as current utils do)
    return require_output_dir_or_exit(ctx, arg_value, log, section=section)


def compute_targets(cli_targets: Sequence[str] | None, sections: Sequence[Dict[str, Any]], log, label: str) -> List[str]:
    """
    Minimal fallback: take CLI if present, otherwise first section that yields non-empty via fallback_targets_from_cfg.
    """
    targets = list(cli_targets or [])
    if targets:
        return targets
    for s in sections:
        targets = fallback_targets_from_cfg([], s, log, label)
        if targets:
            return targets
    return []
    
def add_opt_format(p: argparse.ArgumentParser):
    return p.add_argument("--format", choices=FORMAT_CHOICES, default=argparse.SUPPRESS)

def add_opt_formats(p: argparse.ArgumentParser):
    return p.add_argument("--formats", choices=FORMAT_CHOICES, nargs="*", default=argparse.SUPPRESS)

def add_opt_storage_dir(p: argparse.ArgumentParser, *, for_read: bool):
    h = "directory containing traces (read)" if for_read else "directory where traces will be written"
    return p.add_argument("-s", "--storage-dir", type=Path, default=argparse.SUPPRESS, help=h)

def add_opt_output_dir(p: argparse.ArgumentParser):
    return p.add_argument("-d", "--output-dir", dest="output_dir", type=Path, default=argparse.SUPPRESS,
                          help="directory where outputs will be written")

def add_opt_additional_sys_path(p: argparse.ArgumentParser):
    return p.add_argument("--additional-sys-path", dest="additional_sys_path", nargs="*", default=argparse.SUPPRESS,
                          help="extra import roots; relative paths are resolved under the project root")

def add_opt_targets(p: argparse.ArgumentParser):
    return p.add_argument("--targets", nargs="*", default=argparse.SUPPRESS, metavar="target",
                          help="targets like 'module.func' or 'module.Class.method'")

def add_opt_cmd_remainder(p: argparse.ArgumentParser):
    return p.add_argument("cmd", nargs=argparse.REMAINDER,
                          help="-- then the Python script to run (with its arguments)")

