"""Resolve an address or APN to a Colorado parcel using the statewide
Colorado Public Parcels layer (OIT GIS, ArcGIS REST, no API key). It has 44 of
the 64 counties; the lookup uses it to find a parcel's county.

    python -m states.colorado.resolve "5241 S Rio Grande St, Littleton, CO 80120"
    python -m states.colorado.resolve "<APN or account number>"

Prints a ResolveResult as JSON. With SAVE_RAW=1 every raw response is also
saved to results/raw/.
"""
from __future__ import annotations

import re
import sys
from typing import Optional

import httpx
from shapely.geometry import mapping

from core.common import (SourceUnavailable, clean, geometry_from_rings, iso_date, looks_like_apn,
                         normalize_address, parse_money, query_layer, sql_quote)
from core.models import Observation, ParcelCandidate, ResolveResult

LAYER_URL = (
    "https://gis.colorado.gov/Public/rest/services/Address_and_Parcel/"
    "Colorado_Public_Parcels/MapServer/0/query"
)
SOURCE = "Colorado Public Parcels (OIT statewide composite)"
PAGE_SIZE = 2000  # layer maxRecordCount

# Our field name -> layer field name (copied from the layer's ?f=pjson)
FIELDS = {
    "parcel_id": "parcel_id",
    "account": "account",
    "county": "countyName",
    "county_fips": "countyFips",
    "site_address": "situsAdd",
    "site_city": "sitAddCty",
    "site_zip": "sitAddZip",
    "owner": "owner",
    "owner2": "owner2",
    "owner_mail_street": "ownerAdd",
    "owner_mail_city": "ownAddCty",
    "owner_mail_state": "ownAddStt",
    "owner_mail_zip": "ownAddZip",
    "owner_mail_country": "ownAddCou",
    "legal_description": "legalDesc",
    "land_acres": "landAcres",
    "land_sqft": "landSqft",
    "subdivision_code": "subCode",
    "subdivision_name": "subName",
    "zoning_code": "zoningCode",
    "zoning_desc": "zoningDesc",
    "land_use_code": "landUseCde",
    "land_use_desc": "landUseDsc",
    "last_sale_date": "saleDate",
    "last_sale_price": "salePrice",
    "appraised_total": "apprValTot",
    "assessed_total": "asedValTot",
}
RECORD_URL_FIELD = "URL"
AS_OF_FIELD = "dateReceived"
SHARING_FIELD = "sharing"
MONEY_FIELDS = {"last_sale_price", "appraised_total", "assessed_total"}


def query(params: dict, client: Optional[httpx.Client] = None):
    return query_layer(params, client, url=LAYER_URL, source=SOURCE, page_size=PAGE_SIZE)


def feature_to_candidate(feature: dict, snapshot_id: Optional[str],
                         request_url: str) -> ParcelCandidate:
    attrs = feature.get("attributes", {})
    as_of = iso_date(attrs.get(AS_OF_FIELD))     # the layer gives M/D/YYYY
    record_url = clean(attrs.get(RECORD_URL_FIELD))
    sharing = clean(attrs.get(SHARING_FIELD))
    common = dict(source=SOURCE, url=request_url, snapshot_id=snapshot_id,
                  as_of=as_of, record_url=record_url)

    obs: list[Observation] = []
    for ours, theirs in FIELDS.items():
        value = clean(attrs.get(theirs))
        note = None
        if ours in MONEY_FIELDS and value is not None:
            raw = value
            value = parse_money(raw)
            if value is None:
                note = f"could not parse number from {raw!r}"
            elif ours == "last_sale_price" and value == 0:
                note = "$0 transfer: not a market sale (e.g. quitclaim or related-party transfer)"
        if ours == "owner" and value is not None:
            note = "Primary surface owner (Colorado separates surface and mineral rights)"
        if value is None:
            status = "not_provided"
            note = note or (f"county left this blank (sharing preference: {sharing})"
                            if sharing else "county left this blank")
        else:
            status = "ok"
        obs.append(Observation(field=ours, value=value, status=status, note=note, **common))

    geom = geometry_from_rings(feature.get("geometry", {}).get("rings", []))
    if geom is not None:
        c = geom.centroid
        obs.append(Observation(field="latitude", value=round(c.y, 7),
                               note="parcel polygon centroid", **common))
        obs.append(Observation(field="longitude", value=round(c.x, 7),
                               note="parcel polygon centroid", **common))
        obs.append(Observation(field="parcel_boundary", value=mapping(geom),
                               note="GeoJSON, WGS84", **common))
    else:
        for f in ("latitude", "longitude", "parcel_boundary"):
            obs.append(Observation(field=f, status="not_provided",
                                   note="no geometry returned", **common))

    return ParcelCandidate(
        parcel_id=clean(attrs.get(FIELDS["parcel_id"])),
        account=clean(attrs.get(FIELDS["account"])),
        county=clean(attrs.get(FIELDS["county"])),
        site_address=clean(attrs.get(FIELDS["site_address"])),
        owner=clean(attrs.get(FIELDS["owner"])),
        observations=obs,
    )


def by_apn(apn: str, client: Optional[httpx.Client] = None) -> ResolveResult:
    apn = apn.strip()
    result = ResolveResult(input=apn, input_type="apn", status="not_found")
    tries = [apn]
    stripped = re.sub(r"[\-\.\s]", "", apn)
    if stripped != apn:
        tries.append(stripped)

    for value in tries:
        q = sql_quote(value)
        where = f"{FIELDS['parcel_id']} = '{q}' OR {FIELDS['account']} = '{q}'"
        try:
            data, sid, url = query({"where": where}, client)
        except SourceUnavailable as e:
            result.queries_tried.append({"where": where, "error": str(e)})
            result.status = "source_unavailable"
            result.notes.append(f"{SOURCE} unavailable: {e}")
            return result
        result.queries_tried.append({"where": where, "snapshot_id": sid, "hits": len(data["features"])})
        cands = [feature_to_candidate(f, sid, url) for f in data["features"]]
        if len(cands) == 1:
            result.status, result.parcel = "match", cands[0]
            cands[0].match_reasons.append("exact parcel_id/account match")
            return result
        if len(cands) > 1:
            counties = sorted({c.county or "?" for c in cands})
            result.status, result.candidates = "candidates", cands
            result.notes.append(
                f"{len(cands)} parcels share this number"
                + (f" across counties {counties}: parcel numbers are not unique statewide"
                   if len(counties) > 1 else ""))
            return result

    result.notes.append("no parcel_id or account matched")
    return result


def score_candidate(want: dict, cand: ParcelCandidate) -> tuple[Optional[float], list[str]]:
    """Score how well a parcel's site address matches the input. None = reject."""
    have = normalize_address(cand.site_address or "")
    site_city = next((o.value for o in cand.observations if o.field == "site_city"), None) or ""
    site_zip = next((o.value for o in cand.observations if o.field == "site_zip"), None) or ""
    reasons: list[str] = []

    if have["number"] != want["number"]:
        return None, ["house number differs"]
    if want["name"] and want["name"] not in have["name"] and have["name"] not in want["name"]:
        return None, ["street name differs"]
    score = 2.0
    reasons.append("house number matches")
    if have["name"] == want["name"]:
        score += 3
        reasons.append("street name exact")
    else:
        score += 1
        reasons.append("street name partial")
    for key, plus, minus in (("predir", 1, 3), ("type", 1, 2), ("postdir", 1, 3)):
        if want[key] and have[key]:
            if want[key] == have[key]:
                score += plus
                reasons.append(f"{key} matches")
            else:
                score -= minus
                reasons.append(f"{key} differs ({want[key]} vs {have[key]})")
    if want["zip"] and site_zip[:5] == want["zip"]:
        score += 1
        reasons.append("zip matches")
    if want["city"] and str(site_city).upper() == want["city"]:
        score += 1
        reasons.append("city matches")
    if want["unit"]:
        if have["unit"] == want["unit"]:
            score += 3
            reasons.append("unit matches")
        elif have["unit"]:
            return None, ["unit differs"]
    return score, reasons


def by_address(text: str, client: Optional[httpx.Client] = None) -> ResolveResult:
    result = ResolveResult(input=text, input_type="address", status="not_found")
    want = normalize_address(text)
    if not want["number"] or not want["name"]:
        result.notes.append("could not parse a house number and street name from the input")
        return result

    pattern = f"{want['number']} %{want['name']}%"
    where = f"UPPER({FIELDS['site_address']}) LIKE '{sql_quote(pattern)}'"
    try:
        data, sid, url = query({"where": where}, client)
    except SourceUnavailable as e:
        result.queries_tried.append({"where": where, "error": str(e)})
        result.status = "source_unavailable"
        result.notes.append(f"{SOURCE} unavailable: {e}")
        return result
    result.queries_tried.append({"where": where, "snapshot_id": sid, "hits": len(data["features"])})

    scored: list[ParcelCandidate] = []
    for f in data["features"]:
        cand = feature_to_candidate(f, sid, url)
        s, reasons = score_candidate(want, cand)
        if s is not None:
            cand.score, cand.match_reasons = s, reasons
            scored.append(cand)
    scored.sort(key=lambda c: c.score or 0, reverse=True)

    if not scored:
        result.notes.append(
            f"{len(data['features'])} parcels matched the loose search; none passed address checks"
            if data["features"] else "no parcel has this site address in the statewide layer")
        return result

    best = scored[0].score
    top = [c for c in scored if c.score == best]
    if len(top) == 1 and best >= 5:
        result.status, result.parcel = "match", top[0]
        if len(scored) > 1:
            result.notes.append(f"{len(scored) - 1} weaker candidates ignored")
        return result

    result.status, result.candidates = "candidates", scored
    has_units = any(normalize_address(c.site_address or "")["unit"] for c in top)
    subs = {next((o.value for o in c.observations if o.field == "subdivision_name"), None) for c in top}
    if has_units or len(top) > 1:
        result.notes.append(
            f"{len(top)} parcels match equally well; likely units or condos at one address"
            + (f" ({', '.join(sorted(s for s in subs if s))})" if any(subs) else "")
            + ". Add a unit number or pick one.")
    else:
        result.notes.append("best match is weak; confirm the parcel before using it")
    return result


def resolve(text: str, client: Optional[httpx.Client] = None) -> ResolveResult:
    return by_apn(text, client) if looks_like_apn(text) else by_address(text, client)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python -m states.colorado.resolve "<address or APN>"')
    res = resolve(" ".join(sys.argv[1:]))
    print(res.model_dump_json(indent=2))
