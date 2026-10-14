"""Colorado: what the shared lookup needs from this state (see core/state.py)."""
from __future__ import annotations

from pathlib import Path

from states.colorado.derive import taxcheck
from states.colorado.platforms import arapahoegov
from states.colorado.resolve import resolve
from states.colorado.sos.sos import lookup as entity, site_entities

NAME = "Colorado"
CONFIG_DIR = Path(__file__).resolve().parent / "config"

# Platforms only Colorado uses: Arapahoe County's in-house Assessor and Treasurer sites
PLATFORMS = {
    "arapahoegov.assessor": arapahoegov.assessor,
    "arapahoegov.treasurer": arapahoegov.treasurer,
}
REQUIRED = {
    "arapahoegov.assessor": ["source", "url", "id_param"],
    "arapahoegov.treasurer": ["source", "url", "prior_year_url", "bill_url", "id_param"],
    "co_sos": [],                                  # [entity] in defaults.toml
}

# treasurer platform -> (levies, statement_lines): the tax check's data
TAX_HOOKS = {"arapahoegov.treasurer": (arapahoegov.levies, arapahoegov.statement_lines)}


def tax_check(ctx, treasurer_cfg: dict) -> None:
    """Colorado taxes school levies on a separate school assessed value."""
    levies, statement = TAX_HOOKS.get(treasurer_cfg["platform"], (None, None))
    taxcheck.run(ctx, levies, statement, treasurer_cfg["source"], treasurer_cfg["url"])


# Statement of Authority (C.R.S. 38-30-172), recorded by an entity that holds real
# property; section 5 names "each person authorized to execute instruments ... on
# behalf of the entity". The recorder search returns ~200 characters of scanned text
# around a match, so the names are read forward from these phrases to the end pattern.
AUTHORITY = {
    "doc_types": ["STA"],
    "anchors": ["behalf of the entity is", "on behalf of the entity", "real property on behalf of"],
    "end": (r"\b\d{1,2}\s*[.,:]?\s*The\s+(?:authority|limitations)|\bThe\s+(?:authority|limitations)\b"
            r"|\bSTATE\s+OF\b|\bSubscribed\b|\bOther\s+matters\b"),
    "max_steps": 8,
    "note": ("Statement of Authority recorded by the owner: the person(s) who may sign for it. "
             "authorized is read from the scanned text (OCR); check it against the document"),
    "missing_note": "Colorado does not list LLC members elsewhere",
}

__all__ = ["NAME", "CONFIG_DIR", "PLATFORMS", "REQUIRED", "resolve", "entity", "site_entities",
           "tax_check", "AUTHORITY"]
