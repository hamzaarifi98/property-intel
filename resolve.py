"""Resolve an address or APN to a Colorado parcel using the statewide
Colorado Public Parcels layer (OIT GIS, ArcGIS REST, no API key).

    python resolve.py "5241 S Rio Grande St, Littleton, CO 80120"
    python resolve.py "<APN or account number>"

Prints a ResolveResult as JSON. With SAVE_RAW=1 every raw response is also
saved to results/raw/.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import usaddress
from shapely.geometry import MultiPolygon, Polygon, mapping

from models import Observation, ParcelCandidate, ResolveResult, utcnow

LAYER_URL = (
    "https://gis.colorado.gov/Public/rest/services/Address_and_Parcel/"
    "Colorado_Public_Parcels/MapServer/0/query"
)
SOURCE = "Colorado Public Parcels (OIT statewide composite)"
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", Path(__file__).parent / "results"))
# Raw responses are written only with SAVE_RAW=1 (for debugging a source);
# otherwise a run leaves just its result file (save_result).
SAVE_RAW = os.environ.get("SAVE_RAW") == "1"
RAW_DIR = RESULTS_DIR / "raw"
UNSAFE_NAME_CHARS = r'[\\/:*?"<>|\x00-\x1f]'  # not allowed in Windows/Linux file names
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

DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
              "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
STREET_TYPES = {"STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR",
                "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
                "PLACE": "PL", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "TERRACE": "TER",
                "TRAIL": "TRL", "WAY": "WAY", "PLAZA": "PLZ"}


class SourceUnavailable(Exception):
    """The source could not be reached or returned an error."""


# --------------------------------------------------------------------------- HTTP

def query_layer(params: dict, client: Optional[httpx.Client] = None, url: str = LAYER_URL,
                source: str = SOURCE, page_size: int = PAGE_SIZE,
                post: bool = False) -> tuple[dict, Optional[str], str]:
    """Run one query against an ArcGIS layer (the statewide one by default),
    following pagination.

    Returns (data, snapshot_id, request_url); snapshot_id is None unless
    SAVE_RAW=1 (see save_snapshot). post=True sends the parameters as a form
    body, for geometries too long for a URL; the request URL then has no query
    string.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    base = {"outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json"}
    features: list[dict] = []
    pages: list[dict] = []
    request_url = ""
    try:
        offset = 0
        while True:
            p = {**base, **params, "resultOffset": offset, "resultRecordCount": page_size}
            try:
                r = client.post(url, data=p) if post else client.get(url, params=p)
            except httpx.HTTPError as e:
                raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
            if not request_url:
                request_url = str(r.request.url)
            pages.append({"url": str(r.request.url), "http_status": r.status_code, "body": r.text})
            if r.status_code != 200:
                raise SourceUnavailable(f"HTTP {r.status_code}")
            try:
                data = r.json()
            except ValueError as e:
                raise SourceUnavailable("response was not JSON") from e
            # ArcGIS reports query errors with HTTP 200 and an "error" key
            if "error" in data:
                raise SourceUnavailable(f"ArcGIS error: {data['error']}")
            features.extend(data.get("features", []))
            if not data.get("exceededTransferLimit"):
                break
            offset += page_size
    finally:
        snapshot_id = save_snapshot(params, pages, source)
        if own_client:
            client.close()
    return {"features": features}, snapshot_id, request_url


def save_snapshot(params: dict, pages: list[dict], source: str = SOURCE) -> Optional[str]:
    """Save the raw responses to results/raw/<id>.json when SAVE_RAW=1; returns the id or None."""
    if not SAVE_RAW:
        return None
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    retrieved_at = utcnow().isoformat()
    payload = {"source": source, "params": params, "retrieved_at": retrieved_at, "pages": pages}
    raw = json.dumps(payload, sort_keys=True)
    snapshot_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
    (RAW_DIR / f"{snapshot_id}.json").write_text(raw)
    return snapshot_id


def save_result(subdir: str, label: str, payload: str, time_format: str) -> Path:
    """Write one run's result to results/<subdir>/<label> - <local time>.json.

    `label` is what the user typed (address or id), made safe for a file name.
    Never overwrites: a second run in the same minute gets " (2)", and so on.
    """
    folder = RESULTS_DIR / subdir
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"\s+", " ", re.sub(UNSAFE_NAME_CHARS, " ", label)).strip()[:150] or "result"
    stem = f"{name} - {datetime.now().strftime(time_format)}"
    path, n = folder / f"{stem}.json", 2
    while path.exists():
        path, n = folder / f"{stem} ({n}).json", n + 1
    path.write_text(payload)
    return path


def sql_quote(value: str) -> str:
    """Escape a value for a single-quoted ArcGIS where clause."""
    return value.replace("'", "''")


# ------------------------------------------------------------------ address parsing

def normalize_address(text: str) -> dict:
    """Parse an address into upper-cased, USPS-abbreviated parts."""
    text = text.strip()
    try:
        tagged, _ = usaddress.tag(text)
        parts = dict(tagged)
    except usaddress.RepeatedLabelError:
        parts = {}
    if not parts.get("AddressNumber"):
        m = re.match(r"\s*(\d+)\s+(.+)", text)
        parts = {"AddressNumber": m.group(1), "StreetName": m.group(2).split(",")[0]} if m else {}

    def up(key: str) -> str:
        return re.sub(r"[.,#]", "", parts.get(key, "")).strip().upper()

    predir = up("StreetNamePreDirectional")
    stype = up("StreetNamePostType")
    return {
        "number": up("AddressNumber"),
        "predir": DIRECTIONS.get(predir, predir),
        "name": re.sub(r"\s+", " ", up("StreetName")),
        "type": STREET_TYPES.get(stype, stype),
        "postdir": DIRECTIONS.get(up("StreetNamePostDirectional"), up("StreetNamePostDirectional")),
        "unit": up("OccupancyIdentifier"),
        "city": up("PlaceName"),
        "zip": up("ZipCode")[:5],
    }


def looks_like_apn(text: str) -> bool:
    """An APN has digits and maybe separators, but no street words."""
    t = text.strip()
    return bool(re.fullmatch(r"[A-Za-z]?[\d][\d\-\.\s]{3,}[A-Za-z]?", t))


# ----------------------------------------------------------------- feature -> data

def geometry_from_rings(rings: list) -> Optional[Polygon | MultiPolygon]:
    """Esri rings -> shapely. Esri outer rings are clockwise, holes counter-clockwise."""
    if not rings:
        return None
    outers: list[list] = []
    holes: list[list] = []
    for ring in rings:
        poly = Polygon(ring)
        if not poly.is_valid and poly.area == 0:
            continue
        (holes if poly.exterior.is_ccw else outers).append(ring)
    if not outers:  # odd data: treat every ring as an outer ring
        outers, holes = holes, []
    polys = []
    for outer in outers:
        shell = Polygon(outer)
        inner = [h for h in holes if shell.contains(Polygon(h).representative_point())]
        polys.append(Polygon(outer, inner))
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def iso_date(value: Any) -> Optional[str]:
    """'4/21/2026' or '04/21/2026 00:00:00' -> '2026-04-21'; ISO dates pass through.

    Anything else is returned cleaned and unchanged, so an odd value stays visible.
    """
    s = clean(value)
    if not isinstance(s, str):
        return s
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\b", s)
    if m:
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else s


def parse_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = re.sub(r"[$,\s]", "", str(value))
    try:
        return float(s)
    except ValueError:
        return None


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


# --------------------------------------------------------------------- resolvers

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
            data, sid, url = query_layer({"where": where}, client)
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
        data, sid, url = query_layer({"where": where}, client)
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
        sys.exit('usage: python resolve.py "<address or APN>"')
    res = resolve(" ".join(sys.argv[1:]))
    print(res.model_dump_json(indent=2))
