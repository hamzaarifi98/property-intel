"""Colorado Secretary of State open-data layer.

Looks up a business by name in the Secretary of State's datasets on
data.colorado.gov and returns Observations in the same shape as the county
adapters, so any of them can add the parcel owner's business record to a lookup.

1. Entity (Business Entities in Colorado): matched on the name without
   punctuation. Status, type, formation date, principal address, registered agent.
2. Filing history (Business Entity Transaction History): every filing, and the
   date of the last periodic report.
3. Trade names the entity registered.
4. UCC: secured-debt filings naming the entity as debtor, with lapse date,
   termination and collateral.
5. Related entities: others at the same principal address, each marked when it
   has the same registered agent (names normalized first).
6. Address owner: the owner of the parcel at the principal address, from the
   statewide parcel layer (resolve.by_address). Corroboration only: never a
   contact and not proof of control.

site_entities(address, zip) is separate: entities whose principal address is a
parcel's site address (possible occupants); a county adapter calls it.

Steps 2-6 run only when exactly one entity has the name; a failed step marks
only its own fields source_unavailable.

    python -m core.colorado.sos.sos "5241 S RIO GRANDE STREET LLC"

Each run's result is printed and saved to results/sos/<input> - HH-MM - DD-MM-YY.json.
With SAVE_RAW=1 every raw response is also saved to results/raw/.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from typing import Any, Optional

import httpx

from models import Observation, SourceRecord
from resolve import LAYER_URL, SOURCE as PARCELS_SOURCE
from resolve import SourceUnavailable, by_address, clean, save_result, save_snapshot, sql_quote

from .settings import (SOS_SOURCE, SOCRATA_URL, SOCRATA_LIMIT, RESULTS_SUBDIR,
                       RESULT_TIME_FORMAT, ENTITIES, HISTORY, TRADE_NAMES, UCC_DEBTORS,
                       UCC_FILINGS, UCC_COLLATERAL, SOS_RECORD_URL, ENTITY_WORDS,
                       DIRECTION_WORDS, REPORT_FILINGS, OWN_NAME_FORM, AGENT_RENAME_MIN_WORDS,
                       ENTITY_FIELD, ENTITY_NOTE, FILINGS_NOTE, REPORT_NOTE, TRADE_NAMES_NOTE,
                       NO_TRADE_NAMES_NOTE, UCC_NOTE, RELATED_NOTE, ADDRESS_OWNER_NOTE,
                       AGENT_ADDRESS_NOTE, SITE_ENTITIES_NOTE)


# ------------------------------------------------------------------- Socrata

def socrata(dataset: str, params: dict,
            client: httpx.Client) -> tuple[list[dict], Optional[str], str]:
    """Run one SoQL query on a data.colorado.gov dataset.

    Returns (rows, snapshot_id, request_url); at most SOCRATA_LIMIT rows.
    """
    url = SOCRATA_URL.format(dataset)
    token = os.environ.get("SOCRATA_APP_TOKEN")
    headers = {"X-App-Token": token} if token else {}
    params = {"$limit": str(SOCRATA_LIMIT), **params}
    pages: list[dict] = []
    try:
        try:
            r = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
        pages.append({"url": str(r.request.url), "http_status": r.status_code, "body": r.text})
        if r.status_code != 200:
            raise SourceUnavailable(f"HTTP {r.status_code}")
        try:
            rows = r.json()
        except ValueError as e:
            raise SourceUnavailable("response was not JSON") from e
        # Socrata reports query errors as a JSON object, not a list of rows
        if not isinstance(rows, list):
            raise SourceUnavailable(f"query error: {str(rows)[:200]}")
    finally:
        snapshot_id = save_snapshot(params, pages, SOS_SOURCE)
    return rows, snapshot_id, str(r.request.url)


def capped(note: str, rows: list) -> str:
    return note + (f"; only the first {SOCRATA_LIMIT} rows were read"
                   if len(rows) >= SOCRATA_LIMIT else "")


def fields_status(fields: list[str], status: str, note: str, url: str,
                  snapshot_id: Optional[str] = None) -> list[Observation]:
    return [Observation(field=f, status=status, note=note, source=SOS_SOURCE, url=url,
                        snapshot_id=snapshot_id)
            for f in fields]


def observe(field: str, value: Any, note: str, empty_note: str, common: dict) -> Observation:
    """ok with the value, or not_found (the source answered, nothing there) when empty."""
    if value is None or value == []:
        return Observation(field=field, status="not_found", note=empty_note, **common)
    return Observation(field=field, value=value, note=note, **common)


# ------------------------------------------------------------ names, records

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


def agent_words(name: Optional[str]) -> list[str]:
    """'Folkestad Fazekas Barrick & Patoile, P.C.' -> ['FOLKESTAD', 'FAZEKAS', 'BARRICK', 'PATOILE']."""
    return [w for w in core_words(entity_key(name or "")) if w != "AND"]


def same_agent(a: Optional[str], b: Optional[str]) -> bool:
    """Same registered agent after normalizing, counting a renamed firm as the same.

    A firm that adds a name partner keeps its first words, so one name starting with
    all the words of the other (at least AGENT_RENAME_MIN_WORDS) is the same firm:
    '... Barrick & Patoile, P.C.' = '... Barrick Patoile & James, P.C.'.
    """
    x, y = agent_words(a), agent_words(b)
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    return x == y or (len(short) >= AGENT_RENAME_MIN_WORDS and long_[:len(short)] == short)


def name_pattern(key: str) -> Optional[str]:
    """SoQL LIKE pattern from a name's core words; None if nothing is left."""
    tokens = core_words(key)
    return "%" + "%".join(tokens) + "%" if tokens else None


def day(value: Any) -> Optional[str]:
    """'2015-10-08T00:00:00.000' -> '2015-10-08'."""
    return (clean(value) or "")[:10] or None


def address(row: dict, prefix: str) -> Optional[str]:
    """One-line address from the dataset's <prefix>address1/city/state/zipcode columns."""
    def g(key: str) -> Optional[str]:
        return clean(row.get(prefix + key))
    country = g("country")
    parts = [g("address1"), g("address2"), g("city"),
             " ".join(p for p in (g("state"), g("zipcode")) if p) or None,
             country if country != "US" else None]
    return ", ".join(p for p in parts if p) or None


def person(row: dict, prefix: str) -> Optional[str]:
    return " ".join(p for p in (clean(row.get(f"{prefix}{k}"))
                                for k in ("firstname", "middlename", "lastname", "suffix"))
                    if p) or None


def entity_record(row: dict) -> dict:
    entity_id = clean(row.get("entityid"))
    return {
        "entity_id": entity_id,
        "name": clean(row.get("entityname")),
        "status": clean(row.get("entitystatus")),
        "type": clean(row.get("entitytype")),
        "jurisdiction": clean(row.get("jurisdictonofformation")),   # sic, the dataset's spelling
        "formed": day(row.get("entityformdate")),
        "principal_address": address(row, "principal"),
        "mailing_address": address(row, "mailing"),
        "agent": clean(row.get("agentorganizationname")) or person(row, "agent"),
        "agent_address": address(row, "agentprincipal") or address(row, "agentmailing"),
        "record_url": SOS_RECORD_URL.format(entity_id) if entity_id else None,
    }


# ---------------------------------------------------------------- 1. entity

def lookup(name: str, client: Optional[httpx.Client] = None) -> SourceRecord:
    """Look up one business by name. status: ok (one entity), ambiguous (several,
    none picked), not_found (none, or the name is not a business), source_unavailable."""
    if client is not None:
        return _lookup(name, client)
    with httpx.Client(timeout=30) as own_client:
        return _lookup(name, own_client)


def _lookup(name: str, client: httpx.Client) -> SourceRecord:
    record = SourceRecord(source=SOS_SOURCE, input=name, status="not_found")
    key = entity_key(name)
    pattern = name_pattern(key)
    if not ENTITY_WORDS.search(key) or pattern is None:
        record.notes.append(f"{name!r} does not look like a business name (no LLC, INC, ...); "
                            "not searched")
        return record
    try:
        rows, sid, url = socrata(ENTITIES, {"$where": f"upper(entityname) like '{pattern}'"},
                                 client)
    except SourceUnavailable as e:
        record.status = "source_unavailable"
        record.notes.append(f"entity search failed: {e}")
        record.observations = fields_status([ENTITY_FIELD], "source_unavailable",
                                            f"entity search failed: {e}", SOCRATA_URL.format(ENTITIES))
        return record
    record.url, record.snapshot_id = url, sid
    common = dict(source=SOS_SOURCE, url=url, snapshot_id=sid)
    matches = [r for r in rows if entity_key(r.get("entityname") or "") == key]
    if not matches:
        near = sorted({r["entityname"] for r in rows if r.get("entityname")})[:5]
        record.observations = [Observation(
            field=ENTITY_FIELD, status="not_found",
            note=f"no entity named {name!r}" + (f"; similar: {near}" if near else ""), **common)]
        return record
    if len(matches) > 1:
        record.status = "ambiguous"
        record.observations = [Observation(
            field=ENTITY_FIELD, value=[entity_record(r) for r in matches],
            note=f"{len(matches)} entities have this name; none is picked, so its filings, "
                 f"UCC and address are not looked up. {ENTITY_NOTE}", **common)]
        return record

    row = matches[0]
    entity = entity_record(row)
    record.status = "ok"
    record.observations = [Observation(field=ENTITY_FIELD, value=entity, note=ENTITY_NOTE,
                                       record_url=entity["record_url"], **common)]
    # 2-6, each reports its own failures
    record.observations += filings(entity["entity_id"], client)
    record.observations += trade_names(entity["entity_id"], client)
    record.observations += ucc_filings(key, pattern, client)
    record.observations += principal_address_steps(row, client)
    return record


# --------------------------------------------------------- 2. filing history

def filings(entity_id: str, client: httpx.Client) -> list[Observation]:
    try:
        rows, sid, url = socrata(HISTORY, {"$where": f"entityid = '{sql_quote(entity_id)}'",
                                           "$order": "receiveddate DESC"}, client)
    except SourceUnavailable as e:
        return fields_status(["entity_filings", "entity_last_report"], "source_unavailable",
                             f"filing history: {e}", SOCRATA_URL.format(HISTORY))
    common = dict(source=SOS_SOURCE, url=url, snapshot_id=sid)
    items = [{k: v for k, v in {"date": day(r.get("receiveddate")),
                                "filing": clean(r.get("historydes")),
                                "comment": clean(r.get("comment")),
                                "name": clean(r.get("name")),
                                "transaction_id": clean(r.get("transactionid"))}.items() if v}
             for r in rows]
    report_dates = [i["date"] for i in items if i.get("filing") in REPORT_FILINGS and i.get("date")]
    last_report = report_dates[0] if report_dates else None
    return [observe("entity_filings", items,
                    capped(FILINGS_NOTE, rows) + report_gaps(report_dates),
                    "no filings listed for this entity", common),
            observe("entity_last_report", last_report, REPORT_NOTE,
                    "no periodic report filed yet", common)]


def report_gaps(dates: list[str]) -> str:
    """A note for calendar years with no periodic report between two reports, e.g.
    2021-12-21 then 2023-01-25: nothing dated 2022, so that report was probably late."""
    notes = []
    ordered = sorted(dates)
    for before, after in zip(ordered, ordered[1:]):
        missing = list(range(int(before[:4]) + 1, int(after[:4])))
        if not missing:
            continue
        years = ", ".join(map(str, missing))
        notes.append(f"no report dated {years} (reports on {before} and {after})"
                     + ("; this suggests the report was filed late" if len(missing) == 1
                        and after[:7] <= f"{missing[0] + 1}-06" else ""))
    return (". Gaps (a note, not a flag): " + "; ".join(notes)) if notes else ""


# ------------------------------------------------------------ 3. trade names

def trade_names(entity_id: str, client: httpx.Client) -> list[Observation]:
    try:
        rows, sid, url = socrata(TRADE_NAMES, {"$where": f"entityid = '{sql_quote(entity_id)}'",
                                               "$order": "effectivedate DESC"}, client)
    except SourceUnavailable as e:
        return fields_status(["entity_trade_names"], "source_unavailable", f"trade names: {e}",
                             SOCRATA_URL.format(TRADE_NAMES))
    # The dataset also lists the entity's own name (form "Entity Type"); that is not a DBA
    items = [{"name": clean(r.get("tradenamedescription")),
              "form": clean(r.get("tradenameform")),
              "effective": day(r.get("effectivedate")),
              "status": clean(r.get("entitystatus")),
              "registrant": clean(r.get("registrantorganization")) or person(r, "")}
             for r in rows if clean(r.get("tradenameform")) != OWN_NAME_FORM]
    return [observe("entity_trade_names", items, capped(TRADE_NAMES_NOTE, rows),
                    NO_TRADE_NAMES_NOTE, dict(source=SOS_SOURCE, url=url, snapshot_id=sid))]


# ------------------------------------------------------------------- 4. UCC

def ucc_filings(key: str, pattern: str, client: httpx.Client) -> list[Observation]:
    """One item per UCC file naming the entity as an organization debtor.

    Matched on the name without LLC/INC/...: debtor names on UCC filings often
    drop or vary the suffix ("STATTER RANCH" for STATTER RANCH, INC.). Each item
    carries the debtor name as filed.
    """
    field = "entity_ucc_filings"
    want = core_words(key)
    try:
        debtors, sid, url = socrata(
            UCC_DEBTORS, {"$where": f"upper(organizationname) like '{pattern}'"}, client)
        debtors = [d for d in debtors
                   if core_words(entity_key(d.get("organizationname") or "")) == want]
        file_ids = sorted({d["fileid"] for d in debtors if d.get("fileid")})
        if not file_ids:
            return [Observation(field=field, status="not_found", source=SOS_SOURCE, url=url,
                                snapshot_id=sid,
                                note="no UCC filing names this entity as a debtor")]
        in_ids = ", ".join(f"'{sql_quote(f)}'" for f in file_ids)
        txs, _, _ = socrata(UCC_FILINGS, {"$where": f"fileid in ({in_ids})",
                                          "$order": "filingdate"}, client)
        collateral, _, _ = socrata(UCC_COLLATERAL, {"$where": f"fileid in ({in_ids})"}, client)
    except SourceUnavailable as e:
        return fields_status([field], "source_unavailable", f"UCC: {e}",
                             SOCRATA_URL.format(UCC_DEBTORS))

    today = date.today().isoformat()
    items = []
    for fid in file_ids:
        tx = [t for t in txs if t.get("fileid") == fid]          # oldest first
        first, last = (tx[0], tx[-1]) if tx else ({}, {})
        lapse = max((d for t in tx if (d := day(t.get("lapsedate")))), default=None)
        terminated = any(t.get("terminationflag") is True
                         or "termination" in (t.get("transactiontype") or "").lower() for t in tx)
        described = []
        for c in collateral:
            text = " ".join(p for p in (clean(c.get("collateraldescription")),
                                        clean(c.get("additionalcollateraldescription"))) if p)
            if c.get("fileid") == fid and text and text not in described:
                described.append(text)
        items.append({
            "file_id": fid,
            "debtor_name": sorted({d["organizationname"] for d in debtors
                                   if d.get("fileid") == fid and d.get("organizationname")}),
            "filed": day(first.get("filingdate")),
            "document": clean(first.get("documenttype")),
            "filing_type": clean(first.get("filingtype")),
            "last_action": " ".join(p for p in (clean(last.get("transactiontype")),
                                                day(last.get("filingdate"))) if p) or None,
            "lapse_date": lapse,
            "terminated": terminated,
            "in_effect": not terminated and (lapse is None or lapse >= today),
            "debtor_status": sorted({d.get("recordstatus") for d in debtors
                                     if d.get("fileid") == fid and d.get("recordstatus")}),
            "collateral": "; ".join(described)[:300] or None,
        })
    items.sort(key=lambda i: i["filed"] or "", reverse=True)
    return [Observation(field=field, value=items, note=capped(UCC_NOTE, debtors),
                        source=SOS_SOURCE, url=url, snapshot_id=sid)]


# ------------------------------------------- 5-6. the principal address

def street_key(line: str) -> Optional[tuple[str, str]]:
    """'5241 S. Rio Grande Street' -> ('5241', 'RIO'); None without a street number (PO boxes)."""
    words = entity_key(line).split()
    if not words or not re.fullmatch(r"\d+[A-Z]?", words[0]):
        return None
    street = next((w for w in words[1:] if w not in DIRECTION_WORDS), None)
    return (words[0], street) if street else None


def principal_address_steps(row: dict, client: httpx.Client) -> list[Observation]:
    fields = ["entity_related", "entity_address_owner"]
    line = clean(row.get("principaladdress1"))
    zip5 = (clean(row.get("principalzipcode")) or "")[:5]
    url = SOCRATA_URL.format(ENTITIES)
    if not line or not zip5:
        return fields_status(fields, "not_provided", "no principal address on the entity record",
                             url)
    agent_line = clean(row.get("agentprincipaladdress1")) or ""
    if (entity_key(line) == entity_key(agent_line)
            and zip5 == (clean(row.get("agentprincipalzipcode")) or "")[:5]):
        return fields_status(fields, "not_provided", AGENT_ADDRESS_NOTE, url)
    street = street_key(line)
    if street is None:
        return fields_status(fields, "not_provided",
                             f"principal address {line!r} has no street number (e.g. a PO box)",
                             url)
    return (related_entities(row, street, zip5, client)
            + address_owner(row, line, zip5, client))


def at_address(street: tuple[str, str], zip5: str,
               client: httpx.Client) -> tuple[list[dict], list[dict], Optional[str], str]:
    """Entities whose principal address has this street number, street word and ZIP.

    Returns (matching rows, all rows read, snapshot_id, request_url).
    """
    number, word = street
    where = (f"upper(principaladdress1) like '{number} %{word}%' "
             f"AND principalzipcode like '{zip5}%'")
    rows, sid, url = socrata(ENTITIES, {"$where": where, "$order": "entityname"}, client)
    return ([r for r in rows if street_key(r.get("principaladdress1") or "") == street],
            rows, sid, url)


def entity_summary(r: dict) -> dict:
    return {"entity_id": clean(r.get("entityid")), "name": clean(r.get("entityname")),
            "status": clean(r.get("entitystatus")), "formed": day(r.get("entityformdate")),
            "agent": clean(r.get("agentorganizationname")) or person(r, "agent"),
            "agent_address": address(r, "agentprincipal") or address(r, "agentmailing"),
            "record_url": SOS_RECORD_URL.format(r.get("entityid"))}


def related_entities(row: dict, street: tuple[str, str], zip5: str,
                     client: httpx.Client) -> list[Observation]:
    try:
        matches, rows, sid, url = at_address(street, zip5, client)
    except SourceUnavailable as e:
        return fields_status(["entity_related"], "source_unavailable", f"related entities: {e}",
                             SOCRATA_URL.format(ENTITIES))
    me = row.get("entityid")
    my_agent = clean(row.get("agentorganizationname")) or person(row, "agent")
    items = []
    for r in matches:
        if r.get("entityid") == me:
            continue
        item = entity_summary(r)
        item.pop("agent_address")
        item["same_agent"] = same_agent(item["agent"], my_agent)
        items.append(item)
    return [observe("entity_related", items, capped(RELATED_NOTE, rows),
                    "no other entity has this principal address",
                    dict(source=SOS_SOURCE, url=url, snapshot_id=sid))]


def site_entities(site_address: Optional[str], zip5: Optional[str], client: httpx.Client,
                  exclude: Optional[str] = None) -> list[Observation]:
    """Entities whose principal address is a parcel's site address (possible occupants).

    `exclude` is the owner entity's id, left out of the list.
    """
    field = "site_entities"
    street = street_key(site_address or "")
    zip5 = (zip5 or "")[:5]
    if street is None or not zip5:
        return fields_status([field], "not_provided",
                             f"no street number and ZIP to search ({site_address!r}, {zip5!r})",
                             SOCRATA_URL.format(ENTITIES))
    try:
        matches, rows, sid, url = at_address(street, zip5, client)
    except SourceUnavailable as e:
        return fields_status([field], "source_unavailable", f"entities at the site address: {e}",
                             SOCRATA_URL.format(ENTITIES))
    items = [entity_summary(r) for r in matches if r.get("entityid") != exclude]
    return [observe(field, items, capped(SITE_ENTITIES_NOTE, rows),
                    f"no entity gives {site_address}, {zip5} as its principal address",
                    dict(source=SOS_SOURCE, url=url, snapshot_id=sid))]


def address_owner(row: dict, line: str, zip5: str, client: httpx.Client) -> list[Observation]:
    field = "entity_address_owner"
    state = clean(row.get("principalstate"))
    if state != "CO":
        return fields_status([field], "not_provided",
                             f"principal address is in {state or 'another state'}; the statewide "
                             "parcel layer covers Colorado only", SOCRATA_URL.format(ENTITIES))
    text = f"{line}, {clean(row.get('principalcity')) or ''}, CO {zip5}"
    res = by_address(text, client)            # reports failures in res.status, never raises
    notes = f" ({'; '.join(res.notes)})" if res.notes else ""
    if res.status == "match" and res.parcel:
        parcels = [res.parcel]
    else:                                     # only the best-scoring candidates
        parcels = [c for c in res.candidates if c.score == res.candidates[0].score]
    if not parcels:
        status = "source_unavailable" if res.status == "source_unavailable" else "not_found"
        return [Observation(field=field, status=status, source=PARCELS_SOURCE, url=LAYER_URL,
                            note=f"statewide parcel layer: {res.status} for {text!r}{notes}")]

    def summary(p: Any) -> dict:
        owner2 = next((o.value for o in p.observations
                       if o.field == "owner2" and o.status == "ok"), None)
        return {"owner": p.owner, "owner2": owner2, "county": p.county,
                "parcel_id": p.parcel_id, "site_address": p.site_address}

    # where the owner value came from: the statewide layer (gis.colorado.gov); only the
    # address searched comes from the Secretary of State record
    src = next((o for o in parcels[0].observations if o.field == "owner"), None)
    common = dict(source=PARCELS_SOURCE,
                  url=src.url if src else SOCRATA_URL.format(ENTITIES),
                  snapshot_id=src.snapshot_id if src else None,
                  record_url=src.record_url if src else None, as_of=src.as_of if src else None)
    if len(parcels) == 1:
        return [Observation(field=field, value=summary(parcels[0]),
                            note=f"{ADDRESS_OWNER_NOTE}. Searched {text!r}{notes}", **common)]
    return [Observation(field=field, value=[summary(p) for p in parcels],
                        note=f"{len(parcels)} parcels match {text!r} equally; none is picked. "
                             f"{ADDRESS_OWNER_NOTE}", **common)]


if __name__ == "__main__":
    name = " ".join(sys.argv[1:]).strip()
    if not name:
        sys.exit('usage: python -m core.colorado.sos.sos "<business name>"')
    rec = lookup(name)
    payload = rec.model_dump_json(indent=2)
    print(payload)
    # stderr, so piping the JSON (e.g. into jq) stays clean
    print(f"saved to {save_result(RESULTS_SUBDIR, name, payload, RESULT_TIME_FORMAT)}",
          file=sys.stderr)
