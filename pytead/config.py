# pytead/config.py
from pathlib import Path
from typing import Any, Dict
import argparse
import logging
import os
import importlib.resources as ir

# Exposed for debugging: where the *project-local* config was loaded from (or None)
# This purposely *does not* point to the user-level config to avoid anchoring
# project-specific logic on a home-wide file.
LAST_CONFIG_PATH: Path | None = None

_log = logging.getLogger("pytead.config")


# ---------- File discovery helpers ----------


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


def _find_project_config(start: Path) -> Path | None:
    """
    Return the nearest '.pytead/config.{toml,yaml,yml}' walking upward from 'start'.
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


def _find_user_config() -> Path | None:
    """
    Return the user-level config in precedence order:
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
            import tomli  # optional backport if user has it

            return tomli.loads(txt)
        except Exception as exc:
            _log.warning("Failed to parse TOML with tomli: %s", exc)
            return {}
    except Exception as exc:
        _log.warning("Failed to parse TOML with tomllib: %s", exc)
        return {}


def _load_yaml_text(txt: str) -> Dict[str, Any]:
    try:
        import yaml  # PyYAML
    except Exception as exc:
        _log.warning(
            "Failed to import PyYAML for YAML config parsing: %s (pip install pyyaml)",
            exc,
        )
        return {}
    try:
        data = yaml.safe_load(txt) or {}
        if not isinstance(data, dict):
            _log.warning("YAML config root is not a mapping; ignoring.")
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
    """
    Deep-merge two dicts: values in 'b' override 'a'; nested dicts are merged recursively.
    """
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
    """
    Compute the effective section for a command by merging:
      effective = deep_merge(raw['defaults'] or {}, raw[cmd] or {})
    then coercing types.
    """
    eff = _deep_merge(raw.get("defaults", {}) or {}, raw.get(cmd, {}) or {})
    eff = _coerce_types(eff)
    _log.info("Effective config for [%s]: %s", cmd, eff if eff else "{}")
    return eff


def _is_emptyish(v: Any) -> bool:
    """Treat None, '' and empty list/dict as 'absent' for config fill."""
    if v is None:
        return True
    if isinstance(v, (list, dict, str)) and len(v) == 0:
        return True
    return False


# ---------- Loader (layering: embedded < user < project) ----------


def _load_default_config(start: Path | None = None) -> Dict[str, Any]:
    """
    Layered load:
      base = packaged defaults (pytead/default_config.toml)
      base ← deep-merge user-level config (if any)
      base ← deep-merge nearest project config (if any)

    Returns a single dict that still contains all sections (e.g., 'defaults', 'run', 'gen', ...).
    """
    global LAST_CONFIG_PATH

    # 1) Packaged defaults (TOML)
    base: Dict[str, Any] = {}
    try:
        txt = (
            ir.files("pytead")
            .joinpath("default_config.toml")
            .read_text(encoding="utf-8")
        )
        base = _load_toml_text(txt) or {}
    except Exception as exc:
        _log.info("No packaged defaults available: %s", exc)
        base = {}

    # 2) User-level (global) config
    user_cfg_path = _find_user_config()
    if user_cfg_path:
        try:
            base = _deep_merge(base, _parse_config_file(user_cfg_path))
        except Exception as exc:
            _log.warning("Failed to parse user config %s: %s", user_cfg_path, exc)

    # 3) Project-local (most specific) config
    proj_cfg_path = _find_project_config((start or Path.cwd()).resolve())
    if proj_cfg_path:
        try:
            base = _deep_merge(base, _parse_config_file(proj_cfg_path))
        except Exception as exc:
            _log.warning("Failed to parse project config %s: %s", proj_cfg_path, exc)
        LAST_CONFIG_PATH = proj_cfg_path
    else:
        # Do not anchor project root to a user-level config
        LAST_CONFIG_PATH = None

    return base


# ---------- Public helpers used by CLI code ----------


def apply_config_from_default_file(
    cmd: str, args: argparse.Namespace, start: Path | None = None
) -> None:
    """
    Fill argparse fields that were NOT provided on the CLI using the layered config.
    Additionally, if a field exists but is 'emptyish' (None, [], {}, or ""),
    fill it from config. This fixes the case where argparse created an empty list
    for positionals (e.g., 'targets') that should be provided by config.

    The search for config starts at 'start' (e.g., the script directory) if provided.
    """
    raw = _load_default_config(start)
    if not raw:
        _log.info("No config loaded (no file found).")
        return

    eff = _effective(cmd, raw)

    box = vars(args)
    _log.info("Args BEFORE fill: %s", {k: box[k] for k in sorted(box)})
    for k, v in eff.items():
        if k not in box or _is_emptyish(box[k]):
            box[k] = v
            _log.info("  -> filled '%s' from config: %r", k, v)
    _log.info("Args AFTER  fill: %s", {k: box[k] for k in sorted(box)})


def get_effective_config(cmd: str, start: Path | None = None) -> Dict[str, Any]:
    """
    Return the effective config dict for a given section (e.g. "run", "tead"),
    i.e. merge [defaults] -> [cmd] from the layered configuration (embedded < user < local),
    then coerce types. Does not mutate argparse args.
    The search for config starts at 'start' (e.g., the script directory) if provided.
    """
    raw = _load_default_config(start)
    if not raw:
        _log.info("get_effective_config(%s): no config file found.", cmd)
        return {}
    return _effective(cmd, raw)
