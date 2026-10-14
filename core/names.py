"""Name and address keys used to compare names across sources (owners, parties,
business names, street addresses). Not tied to any state."""
from __future__ import annotations

import re
from typing import Optional

# A name with one of these words is searched as a business
ENTITY_WORDS = re.compile(r"\b(LLC|LLLP|LLP|LP|INC|INCORPORATED|CORP|CORPORATION|COMPANY|"
                          r"LTD|LIMITED|PC|PLLC|PARTNERSHIP|ASSOCIATION)\b")
# Skipped when picking the street word of an address ("5241 S. Rio Grande" -> RIO)
DIRECTION_WORDS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW",
                   "NORTH", "SOUTH", "EAST", "WEST"}


def entity_key(name: str) -> str:
    """'5241 S. Rio Grande Street, LLC' -> '5241 S RIO GRANDE STREET LLC'.

    Periods and apostrophes are dropped (L.L.C. -> LLC); other punctuation
    separates words (MELCOR/TC -> MELCOR TC).
    """
    s = re.sub(r"[.']", "", name.upper().replace("&", " AND "))
    return re.sub(r"[\W_]+", " ", s).strip()


def core_words(key: str) -> list[str]:
    """A name key's words minus LLC/INC/...: 'STATTER RANCH INC' -> ['STATTER', 'RANCH']."""
    return [t for t in key.split() if not ENTITY_WORDS.fullmatch(t)]


def street_key(line: str) -> Optional[tuple[str, str]]:
    """'5241 S. Rio Grande Street' -> ('5241', 'RIO'); None without a street number (PO boxes)."""
    words = entity_key(line).split()
    if not words or not re.fullmatch(r"\d+[A-Z]?", words[0]):
        return None
    street = next((w for w in words[1:] if w not in DIRECTION_WORDS), None)
    return (words[0], street) if street else None
