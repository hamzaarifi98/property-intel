"""States: each one is a folder, states/<slug>/, with a state.py the shared lookup
reads. Everything a state has that other states don't lives in its folder:

    states/<slug>/state.py       what the lookup needs from the state (below)
    states/<slug>/config/        defaults.toml, counties/*.toml, cities/*.toml
    states/<slug>/platforms/     platforms only this state uses (e.g. one county's own site)
    states/<slug>/...            statewide sources (parcel layer, business registry, ...)

Vendor platforms used in many states (ArcGIS, GovOS publicsearch, Accela, eTRAKiT)
stay shared in core/platforms/.

A state.py defines:

    NAME           "Colorado"
    CONFIG_DIR     its config folder
    PLATFORMS      {platform name: step(cfg, ctx)} for its own platforms ({} if none)
    REQUIRED       {platform name: [config keys]} for those platforms
    resolve        resolve(text, client=None) -> ResolveResult: an address or id to a
                   parcel and its county (None if the state has no statewide source yet)
    entity         entity(owner, client) -> SourceRecord: a business owner in the state's
                   business registry (None if not available)
    site_entities  site_entities(address, zip, client, exclude) -> [Observation]
                   (None if not available)
    tax_check      tax_check(ctx, treasurer_cfg) -> None (None if not available)
    AUTHORITY      how recorded documents name who may act for an entity (None if the
                   state has no such document); see core/platforms/publicsearch.py
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

STATES_DIR = Path(__file__).resolve().parent.parent / "states"
DEFAULT = "colorado"


def load(slug: str) -> ModuleType:
    if not (STATES_DIR / slug / "state.py").exists():
        raise ValueError(f"no state {slug!r} (states/{slug}/state.py); available: {available()}")
    return importlib.import_module(f"states.{slug}.state")


def available() -> list[str]:
    return sorted(p.parent.name for p in STATES_DIR.glob("*/state.py"))


def slug_of(module: ModuleType) -> str:
    """states.colorado.state -> 'colorado'."""
    return module.__name__.split(".")[1]
