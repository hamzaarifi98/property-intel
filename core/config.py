"""County and city configs: which platform serves each step, and its settings.

Each state keeps its configs in its own folder (core/state.py):

    states/<state>/config/defaults.toml          sections every county gets unless it has its own
    states/<state>/config/counties/<county>.toml one file per county
    states/<state>/config/cities/<city>.toml     a city's zoning and permits, shared by the
                                                 counties it spans (a county points at it
                                                 from [jurisdictions.<NAME>] city = "...")

A section names its platform (`platform = "publicsearch"`) and gives what that
platform needs (the shared REQUIRED plus the state's own). Configs are checked
when loaded, so a typo fails at once with the file and section, not halfway
through a lookup.

    python -m core.config                  # check Colorado's configs; coverage table
    python -m core.config --state texas
"""
from __future__ import annotations

import re
import sys
import tomllib
from functools import cache
from pathlib import Path

from core import state as states

# shared platform -> keys its section must have; a state adds its own (state.REQUIRED)
REQUIRED: dict[str, list[str]] = {
    "arcgis.parcels": ["source", "url", "fields", "search_fields"],
    "arcgis.value_history": ["source", "url", "id_field", "layers"],
    "arcgis.point": ["source", "url", "fields"],
    "arcgis.flood": ["source", "zones_url", "panels_url"],
    "publicsearch": ["source", "host"],
    "etrakit": ["source", "url"],
    "accela": ["source", "agency"],
}
# the steps a county config can have, in the order a lookup runs them
STEPS = ["parcel", "assessor", "value_history", "treasurer", "jurisdiction", "flood",
         "entity", "recorder"]


class ConfigError(ValueError):
    pass


def slug(name: str) -> str:
    """'El Paso' -> 'el_paso' (the config file name)."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def required(state: str) -> dict[str, list[str]]:
    return {**REQUIRED, **getattr(states.load(state), "REQUIRED", {})}


def config_dir(state: str) -> Path:
    return states.load(state).CONFIG_DIR


def check(section: dict, where: str, state: str) -> None:
    known = required(state)
    platform = section.get("platform")
    if platform not in known:
        raise ConfigError(f"{where}: unknown platform {platform!r}; known: {sorted(known)}")
    missing = [k for k in known[platform] if k not in section]
    if missing:
        raise ConfigError(f"{where}: platform {platform!r} needs {missing}")


def read(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


@cache
def defaults(state: str = states.DEFAULT) -> dict:
    path = config_dir(state) / "defaults.toml"
    cfg = read(path) if path.exists() else {}
    for step in STEPS:
        if step in cfg:
            check(cfg[step], f"{path} [{step}]", state)
    return cfg


@cache
def city(name: str, state: str = states.DEFAULT) -> dict:
    path = config_dir(state) / "cities" / f"{slug(name)}.toml"
    if not path.exists():
        raise ConfigError(f"no city config {path}")
    cfg = read(path)
    for step in ("zoning", "permits"):
        if step in cfg:
            check(cfg[step], f"{path} [{step}]", state)
    return cfg


@cache
def county(name: str, state: str = states.DEFAULT) -> dict:
    path = config_dir(state) / "counties" / f"{slug(name)}.toml"
    if not path.exists():
        raise ConfigError(f"no config for {name} County ({path}); configured: {counties(state)}")
    cfg = read(path)
    cfg.setdefault("name", name)
    if "parcel" not in cfg:
        raise ConfigError(f"{path}: a county needs a [parcel] section")
    for step in STEPS:
        if step in cfg:
            check(cfg[step], f"{path} [{step}]", state)
    for j, jcfg in cfg.get("jurisdictions", {}).items():
        for step in ("zoning", "permits"):
            if step in jcfg:
                check(jcfg[step], f"{path} [jurisdictions.{j}.{step}]", state)
        if "city" in jcfg:
            city(jcfg["city"], state)          # fails now if the city file is missing
    return cfg


def counties(state: str = states.DEFAULT) -> list[str]:
    folder = config_dir(state) / "counties"
    return sorted(read(p).get("name", p.stem) for p in folder.glob("*.toml"))


def coverage(state: str = states.DEFAULT) -> str:
    """Every county config checked, and the platform behind each step (a table)."""
    d = defaults(state)
    rows = [["county", *STEPS, "jurisdictions"]]
    for name in counties(state):
        c = county(name, state)
        cells = [c.get(s, {}).get("platform") or (f"({d[s]['platform']})" if s in d else "-")
                 for s in STEPS]
        rows.append([name, *cells, ", ".join(c.get("jurisdictions", {})) or "-"])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows)


if __name__ == "__main__":
    # (platform) = from the state's defaults.toml
    args = sys.argv[1:]
    st = args[args.index("--state") + 1] if "--state" in args else states.DEFAULT
    print(f"{states.load(st).NAME}  (states available: {', '.join(states.available())})")
    print(coverage(st))
