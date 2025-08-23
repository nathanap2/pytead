# pytead/cli/config_cli.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import os
import logging
import importlib.resources as ir

_log = logging.getLogger("pytead.cli.config")


@dataclass(frozen=True)
class ConfigContext:
    """In-memory representation of the layered configuration."""
    raw: Dict[str, Any]               # full layered mapping with sections (defaults/run/...)
    project_root: Path                # resolved project root (if any), else CWD
    source_path: Optional[Path]       # project-level config file path, if found (else None)


# ---------- File discovery ----------

def _first_existing(paths: list[Path]) -> Optional[Path]:
    for p in paths:
        if p.is_file():
            return p
    return None


def _find_project_config(start: Path) -> Optional[Path]:
    """
    Return nearest '.pytead/config.{toml,yaml,yml}' walking upward from 'start'.
    """
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        base = p / ".pytead"
        cand = _first_existing(
            [base / "config.toml", base / "config.yaml", base / "config.yml"]
        )
        if cand:
            _log.info("project config: %s", cand)
            return cand
    return None


def _find_user_config() -> Optional[Path]:
    """
    User-level precedence:
      1) $PYTEAD_CONFIG            (exact path)
      2) $XDG_CONFIG_HOME/pytead/config.{toml,yaml,yml}
      3) ~/.config/pytead/config.{toml,yaml,yml}
      4) ~/.pytead/config.{toml,yaml,yml}
    """
    env_path = os.getenv("PYTEAD_CONFIG")
    if env_path:
        env_cand = Path(env_path).expanduser()
        if env_cand.is_file():
            _log.info("user config via PYTEAD_CONFIG=%s", env_cand)
            return env_cand

    xdg_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_home:
        cand = _first_existing(
            [
                Path(xdg_home) / "pytead" / "config.toml",
                Path(xdg_home) / "pytead" / "config.yaml",
                Path(xdg_home) / "pytead" / "config.yml",
            ]
        )
        if cand:
            _log.info("user config via XDG: %s", cand)
            return cand

    cand = _first_existing(
        [
            Path.home() / ".config" / "pytead" / "config.toml",
            Path.home() / ".config" / "pytead" / "config.yaml",
            Path.home() / ".config" / "pytead" / "config.yml",
        ]
    )
    if cand:
        _log.info("user config: %s", cand)
        return cand

    cand = _first_existing(
        [
            Path.home() / ".pytead" / "config.toml",
            Path.home() / ".pytead" / "config.yaml",
            Path.home() / ".pytead" / "config.yml",
        ]
    )
    if cand:
        _log.info("user config: %s", cand)
        return cand

    return None


# ---------- Parsers ----------

def _load_toml_text(txt: str) -> Dict[str, Any]:
    try:
        import tomllib  # Python >= 3.11
        return tomllib.loads(txt)
    except ModuleNotFoundError:
        try:
            import tomli
            return tomli.loads(txt)
        except Exception as exc:
            _log.warning("Failed to parse TOML with tomli: %s", exc)
            return {}
    except Exception as exc:
        _log.warning("Failed to parse TOML with tomllib: %s", exc)
        return {}


def _load_yaml_text(txt: str) -> Dict[str, Any]:
    try:
        import yaml
    except Exception as exc:
        _log.warning("PyYAML not available for YAML config parsing: %s", exc)
        return {}
    try:
        data = yaml.safe_load(txt) or {}
        if not isinstance(data, dict):
            _log.warning("YAML root is not a mapping; ignoring.")
            return {}
        return data
    except Exception as exc:
        _log.warning("Failed to parse YAML: %s", exc)
        return {}


def _parse_config_file(path: Path) -> Dict[str, Any]:
    txt = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".toml":
        return _load_toml_text(txt) or {}
    if suffix in (".yaml", ".yml"):
        return _load_yaml_text(txt) or {}
    _log.warning("Unknown config extension '%s' for %s; ignoring.", suffix, path)
    return {}


# ---------- Merging & coercion ----------

def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce_types(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Coerce a few known fields so downstream code gets stable types.
      - Paths: storage_dir, calls_dir, output, output_dir
      - Ints:  limit
      - Lists[str]: formats, gen_formats
      - Targets: list[str]
      - additional_sys_path: list[str]
    """
    from pathlib import Path as _P

    out = dict(d)

    # Paths
    for k in ("storage_dir", "calls_dir", "output", "output_dir"):
        if k in out and isinstance(out[k], str):
            out[k] = _P(out[k]).expanduser()

    # Ints
    if "limit" in out and out["limit"] is not None:
        try:
            out["limit"] = int(out["limit"])
        except Exception:
            pass

    # Lists[str]
    for k in ("formats", "gen_formats"):
        if k in out and out[k] is not None and not isinstance(out[k], (list, tuple)):
            out[k] = [str(out[k])]

    # Targets
    if "targets" in out and out["targets"] is not None:
        val = out["targets"]
        if isinstance(val, str):
            out["targets"] = [val]
        elif isinstance(val, tuple):
            out["targets"] = list(val)
        else:
            try:
                out["targets"] = [str(x) for x in val]
            except TypeError:
                out["targets"] = [str(val)]

    # additional_sys_path → list[str]
    if "additional_sys_path" in out and out["additional_sys_path"] is not None:
        v = out["additional_sys_path"]
        if isinstance(v, (str, Path)):
            out["additional_sys_path"] = [str(v)]
        else:
            out["additional_sys_path"] = [str(x) for x in v]

    return out


def _effective(cmd: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    eff = _deep_merge(raw.get("defaults", {}) or {}, raw.get(cmd, {}) or {})
    eff = _coerce_types(eff)
    _log.info("Effective config for [%s]: %s", cmd, eff if eff else "{}")
    return eff


def _is_emptyish(v: Any) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0)


# ---------- Public API (CLI-only) ----------

def load_layered_config(start: Optional[Path] = None) -> ConfigContext:
    """
    Layered load:
      base = packaged defaults (pytead/default_config.toml)
      base <- user-level config (if any)
      base <- nearest project config from `start` (if any)
    """
    # 1) Packaged defaults
    base: Dict[str, Any] = {}
    try:
        txt = ir.files("pytead").joinpath("default_config.toml").read_text(encoding="utf-8")
        base = _load_toml_text(txt) or {}
    except Exception as exc:
        _log.info("No packaged defaults available: %s", exc)
        base = {}

    # 2) User-level
    user_cfg_path = _find_user_config()
    if user_cfg_path:
        try:
            base = _deep_merge(base, _parse_config_file(user_cfg_path))
        except Exception as exc:
            _log.warning("Failed to parse user config %s: %s", user_cfg_path, exc)

    # 3) Project-level
    source_path = None
    project_root = Path.cwd().resolve()
    proj_cfg_path = _find_project_config((start or Path.cwd()).resolve())
    if proj_cfg_path:
        try:
            base = _deep_merge(base, _parse_config_file(proj_cfg_path))
        except Exception as exc:
            _log.warning("Failed to parse project config %s: %s", proj_cfg_path, exc)
        source_path = proj_cfg_path
        cfg_dir = proj_cfg_path.parent
        project_root = cfg_dir.parent if cfg_dir.name == ".pytead" else cfg_dir

    return ConfigContext(raw=base, project_root=project_root, source_path=source_path)


def effective_section(ctx: ConfigContext, section: str) -> Dict[str, Any]:
    return _effective(section, ctx.raw)


def apply_effective_to_args(section: str, ctx: ConfigContext, args) -> None:
    """
    Fill argparse fields that were NOT provided on the CLI using the effective section.
    Also fill when the field exists but is "emptyish".
    """
    eff = effective_section(ctx, section)
    box = vars(args)
    _log.info("Args BEFORE fill: %s", {k: box[k] for k in sorted(box)})
    for k, v in eff.items():
        if k not in box or _is_emptyish(box[k]):
            box[k] = v
            _log.info("  -> filled '%s' from config: %r", k, v)
    _log.info("Args AFTER  fill: %s", {k: box[k] for k in sorted(box)})

def _resolve_under_project_root(ctx: ConfigContext, p: Path | str | None) -> Path | None:
    """Return an absolute path anchored under ctx.project_root for relative inputs."""
    if p is None:
        return None
    pp = p if isinstance(p, Path) else Path(p).expanduser()
    if not pp.is_absolute():
        pp = ctx.project_root / pp
    try:
        return pp.resolve()
    except Exception:
        return pp

def _path_status(p: Path | None) -> str:
    if p is None:
        return "None"
    try:
        exists = p.exists()
        is_dir = p.is_dir()
        return f"{p} (exists={exists}, is_dir={is_dir})"
    except Exception as exc:
        return f"{p} (stat_error={exc!r})"

def diagnostics_for_storage_dir(ctx: ConfigContext, section: str, cli_value: Path | str | None) -> str:
    """
    Build a human-readable report explaining how storage_dir would be resolved.
    Purely diagnostic; does not mutate anything.
    """
    eff_sec = effective_section(ctx, section) or {}
    eff_def = effective_section(ctx, "defaults") or {}
    eff_typ = effective_section(ctx, "types") or {}

    c_cli  = cli_value
    c_sec  = eff_sec.get("storage_dir")
    c_def  = eff_def.get("storage_dir")
    c_typ  = eff_typ.get("storage_dir")

    r_cli  = _resolve_under_project_root(ctx, c_cli)
    r_sec  = _resolve_under_project_root(ctx, c_sec)
    r_def  = _resolve_under_project_root(ctx, c_def)
    r_typ  = _resolve_under_project_root(ctx, c_typ)

    lines = []
    lines.append("=== pytead GEN diagnostics (storage_dir) ===")
    lines.append(f"cwd           : {Path.cwd().resolve()}")
    lines.append(f"project_root  : {ctx.project_root}")
    lines.append(f"config_source : {ctx.source_path or '<none>'}")
    lines.append("")
    lines.append(f"CLI storage_dir      : {c_cli!r} -> {_path_status(r_cli)}")
    lines.append(f"[{section}].storage_dir : {c_sec!r} -> {_path_status(r_sec)}")
    lines.append(f"[defaults].storage_dir : {c_def!r} -> {_path_status(r_def)}")
    lines.append(f"[types].storage_dir    : {c_typ!r} -> {_path_status(r_typ)}")
    lines.append("")
    lines.append(f"Effective [{section}] section: { {k: eff_sec[k] for k in sorted(eff_sec)} }")
    return "\n".join(lines)
