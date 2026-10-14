"""What every step of a lookup shares: the HTTP client, the configs, and the
observations so far. A platform step reads what earlier steps found
(ctx.found) and appends its own observations (ctx.add)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from core.models import Observation


@dataclass
class Context:
    client: httpx.Client
    county: dict                                  # states/<state>/config/counties/<county>.toml
    defaults: dict                                # states/<state>/config/defaults.toml
    state: Any = None                             # the state module (states/<state>/state.py)
    observations: list[Observation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # set by the parcel step, for the steps after it
    point: Optional[tuple[float, float]] = None   # (x, y) WGS84, inside the parcel
    point_note: str = ""                         # how the point was chosen, for notes
    rings: Optional[list] = None                  # parcel polygon, Esri rings, WGS84
    ids: dict[str, Optional[str]] = field(default_factory=dict)   # parcel_id, account

    def add(self, obs: list[Observation]) -> None:
        self.observations += obs

    def found(self, name: str) -> Optional[Observation]:
        """The first observation of a field with a value (status ok)."""
        return next((o for o in self.observations if o.field == name and o.status == "ok"), None)

    def value(self, name: str) -> Any:
        o = self.found(name)
        return o.value if o else None

    def section(self, name: str) -> Optional[dict]:
        """A county config section, else the state default (e.g. flood from FEMA)."""
        return self.county.get(name) or self.defaults.get(name)
