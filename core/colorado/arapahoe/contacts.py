"""Contact routes: who to reach about a parcel, each with its role and the
observations it rests on. Built from a finished lookup's observations; nothing
is fetched here.

- owner: the owner of record, its mailing address and, for an entity, the
  person(s) named in its Statement of Authority.
- registered_agent: the owner entity's agent for legal notices.
- contractor: from permit contacts, when the permit system shows them.
- occupant: active entities whose principal address is the site address, with
  any permit that names them.

Only routes that were found are emitted; a role with no one found has no route.

The owner of the entity's principal-address parcel (entity_address_owner) is
never a route: it only corroborates the address (see address_owner_note).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from core.colorado.sos.sos import core_words, entity_key, street_key
from models import Observation

INACTIVE = re.compile(r"dissolved|withdrawn|merged|converted|expired", re.I)
FIELD = "contact_routes"


def route(role: str, src: Observation, value: dict, note: str,
          status: str = "ok") -> Observation:
    """One route; source and links are those of its main evidence."""
    return Observation(field=FIELD, status=status, value={"role": role, **value}, note=note,
                       source=src.source, url=src.url, snapshot_id=src.snapshot_id,
                       record_url=src.record_url)


def mailing_address(found: Callable[[str], Optional[Observation]]) -> Optional[str]:
    parts = [found(f"owner_mail_{k}") for k in ("street", "city", "state", "zip")]
    street, city, state, zip_ = (p.value if p else None for p in parts)
    tail = " ".join(x for x in (state, zip_) if x)
    return ", ".join(x for x in (street, city, tail) if x) or None


def contact_routes(obs: list[Observation]) -> list[Observation]:
    def found(field: str) -> Optional[Observation]:
        return next((o for o in obs if o.field == field and o.status == "ok"), None)

    def any_status(field: str) -> Optional[Observation]:
        return next((o for o in obs if o.field == field), None)

    routes: list[Observation] = []
    owner = found("owner")
    entity = found("owner_entity")
    entity_value: dict[str, Any] = entity.value if entity and isinstance(entity.value, dict) else {}
    statements = [o for o in obs if o.field == "statement_of_authority" and o.status == "ok"]

    # owner
    if owner:
        mail = mailing_address(found)
        basis = [f"owner of record: {owner.source}"]
        value: dict[str, Any] = {"name": owner.value, "mailing_address": mail}
        src, note = owner, "the owner of record"
        owners = found("owners")
        if owners and owners.value["type"] in ("individual", "individuals"):
            value["people"] = [p["name"] for p in owners.value["parties"]]
            value["tenancy"] = owners.value.get("tenancy")
            note = "the owner(s) of record, each a person"
        absentee = found("owner_absentee")
        if absentee is not None:
            value["absentee"] = absentee.value
        if entity_value:
            value["principal_address"] = entity_value.get("principal_address")
            if mail and street_key(mail) and \
                    street_key(mail) == street_key(entity_value.get("principal_address") or ""):
                basis.append("the county mailing address is the entity's principal address "
                             "(Secretary of State)")
        sta = statements[0] if statements else None
        persons = (sta.value or {}).get("persons") if sta else None
        if sta:
            v = sta.value
            value["authorized"] = v.get("authorized")
            value["persons"] = persons
            basis.append(f"Statement of Authority {v['instrument']} ({v['recorded']}): "
                         f"{v.get('authorized') or 'names not readable, open the document'}")
            src = sta
            note = ("the owner of record and the person(s) its Statement of Authority names as "
                    "able to act for it; the statement is the record of authority, a mailing "
                    "address is not")
        elif entity_value:
            note += ("; no Statement of Authority was found, so the person behind the entity "
                     "is not known from the records")
        value["basis"] = basis
        routes.append(route("owner", src, value, note))

    # registered agent of the owner entity
    if entity_value.get("agent"):
        routes.append(route("registered_agent", entity, {
            "name": entity_value["agent"], "address": entity_value.get("agent_address"),
            "of": entity_value.get("name"),
            "basis": [f"registered agent on the Secretary of State record of "
                      f"{entity_value.get('name')}"]},
            "accepts legal process and official notices for the owner entity; it is not the "
            "owner's decision-maker"))

    # contractor
    permits = any_status("permits")
    contractors = [p for p in (permits.value or []) if p.get("contractor")] if permits else []
    if contractors:
        latest = contractors[0]
        routes.append(route("contractor", permits, {
            "name": latest["contractor"], "permit": latest["permit_no"],
            "basis": [f"contractor on permit {latest['permit_no']} ({latest.get('applied')})"]},
            "contractor on the most recent permit that names one"))
    # no contractor found: no route (the permits observation says why)

    # occupants: active entities registered at the site address
    site = found("site_entities")
    permit_items = permits.value if permits and permits.status == "ok" else []
    for e in (site.value if site else []):
        if INACTIVE.search(e.get("status") or ""):
            continue
        words = core_words(entity_key(e.get("name") or ""))
        named_in = [p for p in permit_items
                    if words and all(w in entity_key(" ".join(filter(None, (
                        p.get("scope"), p.get("description"), p.get("business_name"))))).split()
                        for w in words)]
        basis = ["Secretary of State principal address is the parcel's site address"]
        basis += [f"named on permit {p['permit_no']} ({p.get('applied')}): {p.get('scope')}"
                  for p in named_in]
        routes.append(route("occupant", site, {
            "name": e.get("name"), "status": e.get("status"), "agent": e.get("agent"),
            "agent_address": e.get("agent_address"), "record_url": e.get("record_url"),
            "basis": basis},
            "a business that gives this parcel as its principal address"
            + (" and that a permit at the parcel names" if named_in else "")
            + "; self-reported, so confirm on site. agent: its registered agent"))
    return routes


def address_owner_note(obs: list[Observation]) -> None:
    """Corroboration on entity_address_owner: a shared surname with the person in the
    Statement of Authority ties the address to that name. Never a contact, and no
    relationship between the two people is inferred."""
    target = next((o for o in obs if o.field == "entity_address_owner" and o.status == "ok"
                   and isinstance(o.value, dict)), None)
    sta = next((o for o in obs if o.field == "statement_of_authority" and o.status == "ok"), None)
    persons = ((sta.value or {}).get("persons") or []) if sta else []
    if not target or not persons:
        return
    owner_words = entity_key(target.value.get("owner") or "").split()
    for p in persons:
        name_words = entity_key(p["name"]).split()
        surname = name_words[-1] if name_words else None
        if not surname or surname not in owner_words:
            continue
        if sorted(owner_words) == sorted(name_words):
            target.note += (f". The address owner is the person named in the Statement of "
                            f"Authority ({p['name']})")
        else:
            target.note += (f". Corroboration: {target.value['owner']} shares the surname "
                            f"{surname} with {p['name']} (Statement of Authority "
                            f"{sta.value['instrument']}), which ties the principal address to "
                            f"the {surname.title()} name. {target.value['owner']} is a different "
                            f"person from {p['name']}: not a contact, and no relationship "
                            f"between them is inferred")
        return
