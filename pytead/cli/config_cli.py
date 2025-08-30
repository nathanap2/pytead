# pytead/cli/config_cli.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, List
import os
import logging
import hashlib
import importlib.resources as ir
import re

_log = logging.getLogger("pytead.cli.config")

# ------------------ Buffer d'événements de debug (local au module) ------------------

_DEBUG_EVENTS: List[Dict[str, Any]] = []

def _dbg(kind: str, **details: Any) -> None:
    """
    Empile un événement structuré (non loggé tout de suite) pour pouvoir
    reconstruire un rapport détaillé *au moment où* on en a besoin.
    """
    try:
        _DEBUG_EVENTS.append({"kind": kind, **details})
    except Exception:
        # ne jamais casser la charge config pour de la télémétrie
        pass

_SECRET_KEY_RE = re.compile(r"(?i)\b(token|secret|password|passwd|apikey|api_key|auth|key)\b")

def _redact_preview(s: str, max_chars: int = 1200) -> str:
    """
    Coupe le texte et masque naïvement les valeurs de lignes sensibles.
    """
    s = s[:max_chars]
    out = []
    for line in s.splitlines():
        if _SECRET_KEY_RE.search(line):
            if "=" in line:
                k, _ = line.split("=", 1)
                line = f"{k}= ***REDACTED***"
            elif ":" in line:
                k, _ = line.split(":", 1)
                line = f"{k}: ***REDACTED***"
            else:
                line = "***REDACTED***"
        out.append(line)
    return "\n".join(out)

def _sha256_bytes(b: bytes) -> str:
    try:
        return hashlib.sha256(b).hexdigest()
    except Exception:
        return "<sha256-error>"

# ------------------ Modèle de contexte ------------------

@dataclass(frozen=True)
class ConfigContext:
    """In-memory representation of the layered configuration."""
    raw: Dict[str, Any]               # full layered mapping with sections (defaults/run/...)
    project_root: Path                # resolved project root (if any), else CWD
    source_path: Optional[Path]       # project-level config file path, if found (else None)
    debug: List[Dict[str, Any]] = field(default_factory=list)  # événements capturés

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
    _dbg("project_search_start", start=str(cur))
    for p in [cur, *cur.parents]:
        base = p / ".pytead"
        cands = [base / "config.toml", base / "config.yaml", base / "config.yml"]
        _dbg("project_search_try", dir=str(p), candidates=[str(x) for x in cands])
        cand = _first_existing(cands)
        if cand:
            _dbg("project_config_found", path=str(cand))
            _log.info("project config: %s", cand)
            return cand
    _dbg("project_config_not_found")
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
        _dbg("user_env_candidate", env="PYTEAD_CONFIG", value=env_path, exists=env_cand.is_file())
        if env_cand.is_file():
            _log.info("user config via PYTEAD_CONFIG=%s", env_cand)
            return env_cand

    xdg_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_home:
        paths = [
            Path(xdg_home) / "pytead" / "config.toml",
            Path(xdg_home) / "pytead" / "config.yaml",
            Path(xdg_home) / "pytead" / "config.yml",
        ]
        _dbg("user_xdg_candidates", base=xdg_home, candidates=[str(x) for x in paths])
        cand = _first_existing(paths)
        if cand:
            _log.info("user config via XDG: %s", cand)
            return cand

    paths = [
        Path.home() / ".config" / "pytead" / "config.toml",
        Path.home() / ".config" / "pytead" / "config.yaml",
        Path.home() / ".config" / "pytead" / "config.yml",
    ]
    _dbg("user_home_candidates", candidates=[str(x) for x in paths])
    cand = _first_existing(paths)
    if cand:
        _log.info("user config: %s", cand)
        return cand

    paths = [
        Path.home() / ".pytead" / "config.toml",
        Path.home() / ".pytead" / "config.yaml",
        Path.home() / ".pytead" / "config.yml",
    ]
    _dbg("user_legacy_candidates", candidates=[str(x) for x in paths])
    cand = _first_existing(paths)
    if cand:
        _log.info("user config: %s", cand)
        return cand

    _dbg("user_config_not_found")
    return None

# ---------- Parsers ----------

def _load_toml_text(txt: str) -> Dict[str, Any]:
    """
    Essaie tomllib (3.11+), puis tomli (3.9/3.10), puis toml (si installé).
    Trace chaque tentative (ok/erreur).
    """
    try:
        import tomllib  # Python >= 3.11
        _dbg("toml_parser_try", lib="tomllib")
        try:
            out = tomllib.loads(txt)
            _dbg("toml_parser_ok", lib="tomllib")
            return out
        except Exception as exc:
            _dbg("toml_parser_fail", lib="tomllib", error=str(exc))
            _log.warning("Failed to parse TOML with tomllib: %s", exc)
    except ModuleNotFoundError:
        _dbg("toml_lib_missing", lib="tomllib")

    try:
        import tomli  # 3.9 / 3.10
        _dbg("toml_parser_try", lib="tomli")
        out = tomli.loads(txt)
        _dbg("toml_parser_ok", lib="tomli")
        return out
    except Exception as exc:
        _dbg("toml_parser_fail", lib="tomli", error=str(exc))
        _log.warning("Failed to parse TOML with tomli: %s", exc)

    try:
        import toml  # optionnel
        _dbg("toml_parser_try", lib="toml")
        out = toml.loads(txt)
        _dbg("toml_parser_ok", lib="toml")
        return out
    except Exception as exc:
        _dbg("toml_parser_fail", lib="toml", error=str(exc))
        _log.warning("Failed to parse TOML with toml: %s", exc)

    _dbg("toml_parser_all_failed")
    return {}

def _load_yaml_text(txt: str) -> Dict[str, Any]:
    try:
        import yaml
    except Exception as exc:
        _dbg("yaml_lib_missing", error=str(exc))
        _log.warning("PyYAML not available for YAML config parsing: %s", exc)
        return {}
    try:
        _dbg("yaml_parser_try", lib="pyyaml")
        data = yaml.safe_load(txt) or {}
        if not isinstance(data, dict):
            _dbg("yaml_parser_non_mapping")
            _log.warning("YAML root is not a mapping; ignoring.")
            return {}
        _dbg("yaml_parser_ok")
        return data
    except Exception as exc:
        _dbg("yaml_parser_fail", error=str(exc))
        _log.warning("Failed to parse YAML: %s", exc)
        return {}

def _parse_config_file(path: Path) -> Dict[str, Any]:
    """
    Lit le fichier, loggue taille/sha256 + aperçu masqué, puis parse selon l’extension.
    """
    try:
        b = path.read_bytes()
    except Exception as exc:
        _dbg("config_read_error", path=str(path), error=str(exc))
        return {}
    try:
        txt = b.decode("utf-8")
    except Exception as exc:
        _dbg("config_decode_error", path=str(path), size=len(b), sha256=_sha256_bytes(b), error=str(exc))
        return {}
    _dbg("config_read_ok", path=str(path), size=len(b), sha256=_sha256_bytes(b), preview=_redact_preview(txt))

    suffix = path.suffix.lower()
    if suffix == ".toml":
        out = _load_toml_text(txt) or {}
    elif suffix in (".yaml", ".yml"):
        out = _load_yaml_text(txt) or {}
    else:
        _dbg("config_unknown_extension", path=str(path), ext=suffix)
        _log.warning("Unknown config extension '%s' for %s; ignoring.", suffix, path)
        out = {}

    _dbg("config_parsed", path=str(path), keys=sorted(list(out.keys())) if isinstance(out, dict) else "<non-dict>")
    return out

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
    Coerce quelques champs connus pour donner des types stables en aval.
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
    _dbg("effective_section", section=cmd, keys=sorted(list(eff.keys())))
    _log.info("Effective config for [%s]: %s", cmd, eff if eff else "{}")
    return eff

def _is_emptyish(v: Any) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and len(v) == 0)

# ---------- Public API (CLI-only) ----------

def load_layered_config(start: Optional[Path] = None) -> ConfigContext:
    """
    Layered load:
      base = packaged defaults (pytead/default_config.toml) — si présent
      base <- user-level config (if any)
      base <- nearest project config from `start` (if any)
    """
    # Reset du buffer d’événements
    global _DEBUG_EVENTS
    _DEBUG_EVENTS = []

    # 1) Packagé
    base: Dict[str, Any] = {}
    try:
        txt = ir.files("pytead").joinpath("default_config.toml").read_text(encoding="utf-8")
        _dbg("packaged_default_found", path="pytead/default_config.toml", size=len(txt))
        pkg = _load_toml_text(txt) or {}
        base = _deep_merge(base, pkg)
    except Exception as exc:
        _dbg("packaged_default_missing", error=str(exc))
        _log.info("No packaged defaults available: %s", exc)
        base = {}

    # 2) User-level
    user_cfg_path = _find_user_config()
    if user_cfg_path:
        try:
            data = _parse_config_file(user_cfg_path)
            base = _deep_merge(base, data)
            _dbg("user_config_merged", path=str(user_cfg_path), top_keys=sorted(list(data.keys())))
        except Exception as exc:
            _dbg("user_config_parse_error", path=str(user_cfg_path), error=str(exc))
            _log.warning("Failed to parse user config %s: %s", user_cfg_path, exc)

    # 3) Project-level
    source_path = None
    project_root = Path.cwd().resolve()
    proj_cfg_path = _find_project_config((start or Path.cwd()).resolve())
    if proj_cfg_path:
        try:
            data = _parse_config_file(proj_cfg_path)
            base = _deep_merge(base, data)
            _dbg("project_config_merged", path=str(proj_cfg_path), top_keys=sorted(list(data.keys())))
        except Exception as exc:
            _dbg("project_config_parse_error", path=str(proj_cfg_path), error=str(exc))
            _log.warning("Failed to parse project config %s: %s", proj_cfg_path, exc)
        source_path = proj_cfg_path
        cfg_dir = proj_cfg_path.parent
        project_root = cfg_dir.parent if cfg_dir.name == ".pytead" else cfg_dir

    ctx = ConfigContext(raw=base, project_root=project_root, source_path=source_path, debug=list(_DEBUG_EVENTS))
    _dbg("ctx_summary", project_root=str(project_root), source=str(source_path or "<none>"),
         top_keys=sorted(list(base.keys())))
    return ctx

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
            _dbg("arg_filled", section=section, name=k, value=str(v))
            _log.info("  -> filled '%s' from config: %r", k, v)
    _log.info("Args AFTER  fill: %s", {k: box[k] for k in sorted(box)})

def resolve_under_project_root(ctx: ConfigContext, p: Path | str | None) -> Path | None:
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

def render_config_debug_report(ctx: ConfigContext, include_previews: bool = True) -> str:
    """
    Rapport détaillé retraçant découverte, ouverture, parsing (y compris erreurs),
    et sections effectives.
    """
    lines: List[str] = []
    lines.append("=== pytead CONFIG DEBUG REPORT ===")
    lines.append(f"cwd          : {Path.cwd().resolve()}")
    lines.append(f"project_root : {ctx.project_root}")
    lines.append(f"config_source: {ctx.source_path or '<none>'}")
    lines.append("")

    def fmt(ev: Dict[str, Any]) -> str:
        d = dict(ev)
        kind = d.pop("kind", "?")
        body = "\n".join(f"    {k}: {d[k]}" for k in sorted(d))
        return f"- {kind}\n{body}" if body else f"- {kind}"

    events = []
    for ev in ctx.debug or []:
        evc = dict(ev)
        if not include_previews and "preview" in evc:
            evc["preview"] = "<omitted>"
        events.append(evc)

    if not events:
        lines.append("(no debug events captured)")
    else:
        lines.append("Events:")
        lines.extend(fmt(e) for e in events)

    lines.append("")
    lines.append("Top-level keys in layered config: " + ", ".join(sorted(ctx.raw.keys())))
    for sec in ("defaults", "run", "gen", "tead", "types"):
        eff = _effective(sec, ctx.raw)
        keys = ", ".join(sorted(eff.keys())) if eff else "<empty>"
        lines.append(f"Effective [{sec}] keys: {keys}")
    return "\n".join(lines)

def diagnostics_for_storage_dir(ctx: ConfigContext, section: str, cli_value: Path | str | None) -> str:
    """
    Rapport humain enrichi (résolution du storage_dir) + **rapport complet de config**.
    Ce texte est déjà affiché par les commandes quand ça plante.
    """
    eff_sec = effective_section(ctx, section) or {}
    eff_def = effective_section(ctx, "defaults") or {}
    eff_typ = effective_section(ctx, "types") or {}

    c_cli  = cli_value
    c_sec  = eff_sec.get("storage_dir")
    c_def  = eff_def.get("storage_dir")
    c_typ  = eff_typ.get("storage_dir")

    r_cli  = resolve_under_project_root(ctx, c_cli)
    r_sec  = resolve_under_project_root(ctx, c_sec)
    r_def  = resolve_under_project_root(ctx, c_def)
    r_typ  = resolve_under_project_root(ctx, c_typ)

    lines: List[str] = []
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
    lines.append("Effective sections snapshot:")
    for sec in ("defaults", section, "types"):
        eff = effective_section(ctx, sec)
        keys = ", ".join(sorted(eff.keys())) if eff else "<empty>"
        lines.append(f"  - [{sec}] keys: {keys}")

    # --- Ajout : rapport complet (découverte/lecture/parsing) ---
    lines.append("")
    lines.append(render_config_debug_report(ctx, include_previews=True))

    return "\n".join(lines)

