"""Owner names: split an owner of record into its parties.

County records write several owners in one string, people as LAST FIRST MIDDLE:
'TURNER JASON, TURNER KATELYN' or 'SMITH JOHN & MARY'. Searches (the Clerk &
Recorder indexes each party on its own) and contact routes need the parties.

    split_owner("TURNER JASON, TURNER KATELYN")
    -> {"type": "individuals", "parties": [{"as_recorded": "TURNER JASON",
        "name": "Jason Turner", "type": "individual"}, ...]}

A business or trust name is one party and is never split ('SMITH & JONES LLC').
"""
from __future__ import annotations

import re

from core.names import ENTITY_WORDS

TRUST_WORDS = re.compile(r"\b(TRUST|TRUSTEE|TRUSTEES|TR|ESTATE)\b")
SEPARATORS = re.compile(r"\s*(?:,|&|\bAND\b)\s*", re.I)
SUFFIXES = {"JR", "SR", "II", "III", "IV"}
# "and others", "and wife", "and husband": not part of the name
OTHERS = re.compile(r"\s*\b(ET\s*AL|ETAL|ET\s*UX|ET\s*VIR)\b\.?", re.I)


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.upper().replace("&", " AND ")).split()


def display_name(last_first: str) -> str:
    """'TURNER JASON A' -> 'Jason A Turner'; 'RAINS GLENN JR' -> 'Glenn Rains Jr'."""
    parts = last_first.split()
    suffix = [p for p in parts[1:] if p.upper().rstrip(".") in SUFFIXES]
    rest = [p for p in parts[1:] if p not in suffix]
    return " ".join(rest + parts[:1] + suffix).title() if len(parts) > 1 else last_first.title()


def split_owner(owner: str) -> dict:
    """Parties of an owner of record and its type: entity, trust, individual(s)."""
    text = re.sub(r"\s+", " ", owner or "").strip()
    w = " ".join(words(text))
    if ENTITY_WORDS.search(w):
        return {"type": "entity", "parties": [{"as_recorded": text, "name": text,
                                               "type": "entity"}]}
    if TRUST_WORDS.search(w):
        return {"type": "trust", "parties": [{"as_recorded": text, "name": text,
                                              "type": "trust"}]}
    parties, surname = [], None
    for part in (p.strip() for p in SEPARATORS.split(text)):
        # 'BUSHUE DARLENE ET AL': others share the title but are not named here
        others = bool(OTHERS.search(part))
        part = OTHERS.sub("", part).strip()
        if not part:
            continue
        if len(part.split()) == 1 and surname:   # 'SMITH JOHN & MARY': MARY takes SMITH
            part = f"{surname} {part}"
        surname = part.split()[0]
        party = {"as_recorded": part, "name": display_name(part), "type": "individual"}
        if others:
            party["et_al"] = True                # and others, not named in the owner field
        parties.append(party)
    return {"type": "individual" if len(parties) == 1 else "individuals", "parties": parties}
