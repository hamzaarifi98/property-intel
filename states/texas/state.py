"""Texas: what the shared lookup needs from this state (see core/state.py).

Nothing is built yet. Fill these in as Texas sources are added; until then a lookup
needs --county and a county id, and runs only the configured county steps.
"""
from __future__ import annotations

from pathlib import Path

NAME = "Texas"
CONFIG_DIR = Path(__file__).resolve().parent / "config"

PLATFORMS: dict = {}          # platforms only Texas uses (e.g. a county's own site)
REQUIRED: dict = {}           # their config keys

resolve = None                # address or id -> parcel and county (a statewide parcel source)
entity = None                 # business owner lookup (Texas Secretary of State / Comptroller)
site_entities = None          # businesses registered at an address
tax_check = None              # does the bill follow from values and rates
AUTHORITY = None              # a recorded document naming who may act for an entity
