from pathlib import Path
from typing import Any, Dict
import argparse
import logging
import os
import importlib.resources as ir

# Exposed for debugging: where the config was loaded from (or None)
LAST_CONFIG_PATH: Path | None = None

_log = logging.getLogger("pytead.config")


def _find_default_config(start: Path) -> Path | None:
    """
    Search for a project-local or user-level config file.

    Project-local (searched upward from 'start'):
      - .pytead/config.toml

    User-level (fallback):
      - $PYTEAD_CONFIG
      - $XDG_CONFIG_HOME/pytead/config.toml
      - ~/.config/pytead/config.toml
      - ~/.pytead/config.toml
    """
    cur = start.resolve()
    _log.info("Searching for default config starting at %s ...", cur)

    # 1) Project-local, walking up parents
    for p in [cur, *cur.parents]:
        cand = p / ".pytead" / "config.toml"
        if cand.is_file():
            _log.info("  -> found config at %s", cand)
            return cand

    # 2) User-level overrides / fallbacks
    env_path = os.getenv("PYTEAD_CONFIG")
    if env_path:
        env_cand = Path(env_path).expanduser()
        if env_cand.is_file():
            _log.info("  -> using PYTEAD_CONFIG=%s", env_cand)
            return env_cand

    xdg_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_home:
        xdg_cand = Path(xdg_home) / "pytead" / "config.toml"
        if xdg_cand.is_file():
            _log.info("  -> found user config at %s", xdg_cand)
            return xdg_cand

    cfg_cand = Path.home() / ".config" / "pytead" / "config.toml"
    if cfg_cand.is_file():
        _log.info("  -> found user config at %s", cfg_cand)
        return cfg_cand

    home_cand = Path.home() / ".pytead" / "config.toml"
    if home_cand.is_file():
        _log.info("  -> found user config at %s", home_cand)
        return home_cand

    _log.info("No config file found.")
    return None


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
    
def _load_default_config() -> Dict[str, Any]:
    global LAST_CONFIG_PATH
    path = _find_default_config(Path.cwd())
    if path:
        LAST_CONFIG_PATH = path
        txt = path.read_text(encoding="utf-8")
        return _load_toml_text(txt) or {}

    try:
        txt = ir.files("pytead").joinpath("default_config.toml").read_text(encoding="utf-8")
        LAST_CONFIG_PATH = None  # explicite: fallback interne
        return _load_toml_text(txt) or {}
    except Exception:
        return {}



def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Values in 'b' override 'a'; dicts are merged recursively."""
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
      - Ints: limit
      - Lists[str]: formats, gen_formats
      - Targets: list[str]
    """
    from pathlib import Path as _P

    out = dict(d)

    # Paths
    for k in ("storage_dir", "calls_dir", "output", "output_dir"):
        if k in out and isinstance(out[k], str):
            out[k] = _P(out[k]).expanduser()

    # Ints
    if "limit" in out and out["limit"] is not None:
        out["limit"] = int(out["limit"])

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

    return out


def _effective(cmd: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Merge [defaults] -> [cmd], then coerce types."""
    eff = _deep_merge(raw.get("defaults", {}), raw.get(cmd, {}))
    eff = _coerce_types(eff)
    _log.info("Effective config for [%s]: %s", cmd, eff if eff else "{}")
    return eff


def _is_emptyish(v: Any) -> bool:
    """Treat None, empty list/dict/str as 'absent' for config fill."""
    if v is None:
        return True
    if isinstance(v, (list, dict, str)) and len(v) == 0:
        return True
    return False


def apply_config_from_default_file(cmd: str, args: argparse.Namespace) -> None:
    """
    Fill argparse fields that were NOT provided on the CLI.
    Additionally, if a field exists but is 'emptyish' (None, [], {}, or ""),
    fill it from config. This fixes the case where argparse created an empty list
    for positionals (e.g., 'targets') that should be provided by config.
    """
    raw = _load_default_config()
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


def get_effective_config(cmd: str) -> Dict[str, Any]:
    """
    Return the effective config dict for a given section (e.g. "run", "tead"),
    i.e. merge [defaults] -> [cmd] and coerce types. Does not mutate argparse args.
    """
    raw = _load_default_config()
    if not raw:
        _log.info("get_effective_config(%s): no config file found.", cmd)
        return {}
    return _effective(cmd, raw)
