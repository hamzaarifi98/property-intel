"""Arapahoe County Assessor adapter.

Looks up one parcel by AIN or PIN and returns Observations in the same shape
as resolve.py, so the county's current values can be set next to the
statewide layer's yearly ones.

1. County GIS (primary, JSON): the parcel layer of the county's ArcGIS server,
   InfoService/MapServer/12 (all ~234k parcels; the layer the county's
   ArapaMAP viewer uses). Owner, mailing address, values, last sale, location.
2. Assessor parcel page (detail, HTML): parcelsearch.arapahoegov.com/PPINum.aspx.
   Only what the layer lacks: sales history, buildings, areas, land lines,
   assessed split, school value, mill levy, legal description. The page has
   no API; values are read by the ids of its ASP.NET labels.
3. Value history (JSON): the Assessor's value-change layers on the county GIS
   (CustomCAMA_WM), one appraised value per tax year back to 2016.
4. Treasurer (HTML): taxsearch.arapahoegov.com. Billed, paid and due tax,
   delinquency and lien flags, and the prior year's tax.
5. Location (JSON): jurisdiction, zoning (City of Littleton's GIS, or the
   county's for unincorporated land) and FEMA flood zone, by point and polygon
   queries against the parcel.
6. Owner entity (JSON): when the owner is a business, the statewide Secretary
   of State open-data layer (core/colorado/sos): entity record, filing history,
   trade names, UCC filings, other entities at its principal address and the
   owner of that address.
7. Clerk & Recorder (WebSocket, recorder.py): documents naming the owner, the
   deed that conveyed the parcel to it, and its Statement of Authority with the
   person(s) who may act for it.
8. Permits: the City of Littleton's eTRAKiT (core/colorado/littleton) when the
   parcel is in Littleton.
9. Site entities: businesses whose Secretary of State principal address is the
   site address (possible occupants).
10. Contact routes (contacts.py): owner, registered agent, contractor and
    occupant, each with its role and evidence, from the observations above.

Steps 2-10 run only with detail=True; a failed step marks its own fields
source_unavailable and never fails the lookup.

    python -m core.colorado.arapahoe.arapahoe 2077-16-2-22-004          # AIN (statewide parcel_id)
    python -m core.colorado.arapahoe.arapahoe 034472207                 # PIN
    python -m core.colorado.arapahoe.arapahoe "5241 S Rio Grande St, Littleton, CO 80120"
    python -m core.colorado.arapahoe.arapahoe 2077-16-2-22-004 --no-detail

Each run's result is printed and saved to results/arapahoe/<input> - HH-MM - DD-MM-YY.json.
With SAVE_RAW=1 every raw response is also saved to results/raw/.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup, Tag

from core.colorado.littleton.permits import permits as littleton_permits
from core.colorado.sos.sos import lookup as sos_lookup, site_entities
from models import Observation, SourceRecord
from owners import split_owner
from resolve import (SourceUnavailable, clean, looks_like_apn, normalize_address, parse_money,
                     query_layer, resolve, save_result, save_snapshot, sql_quote)

from .settings import (COUNTY, SOURCE, RESULTS_SUBDIR, RESULT_TIME_FORMAT,
                       GIS_URL, GIS_SOURCE, GIS_PAGE_SIZE, GIS_FIELDS,
                       DATE_FIELDS, GIS_NOTES, DEED_SEARCH_URL, PAGE_URL, PAGE_SOURCE,
                       HEADERS, NOT_FOUND_TEXT, FOUND_MARKER, HEADER_FIELDS, NUMBER_FIELDS,
                       VALUE_GROUPS, DETAIL_FIELDS, CAMA_URL, HISTORY_SOURCE,
                       VALUE_CHANGE_LAYERS, TREASURER_URL, PRIOR_YEAR_URL, TAX_BILL_URL,
                       TREASURER_SOURCE, TREASURER_FOUND, TREASURER_NOT_FOUND,
                       TREASURER_NOTE, TAX_MONEY_FIELDS, TAX_FLAGS, PRIOR_MONEY_FIELDS,
                       TAX_FIELDS, PRIOR_TAX_FIELDS, JURISDICTION_URL, COUNTY_ZONING_URL,
                       FLOOD_URL, FLOOD_PANEL_URL, LOCATION_SOURCE, LITTLETON_ZONING_URL,
                       LITTLETON_SOURCE, NO_BFE, POINT_NOTE, ZONING_FIELDS, FLOOD_FIELDS,
                       FLOOD_PANEL_FIELDS, LIVING_AREA, NOT_LIVING_AREA, BASEMENT,
                       BASEMENT_TOTAL, SQFT_PER_ACRE, PERMIT_ISSUERS)
from .contacts import address_owner_note, contact_routes
from .permits import county_permits
from .recorder import recorder_observations
from .taxcheck import tax_check


# --------------------------------------------------------------------- input ids

def normalize_key(text: str) -> tuple[str, str]:
    """Return (kind, value): ('ain', '2077-16-2-22-004') or ('pin', '034472207').

    Raises ValueError for anything else.
    """
    t = re.sub(r"\s+", "", text)
    digits = re.sub(r"\D", "", t)
    if len(digits) == 12 and re.fullmatch(r"[\d\-\.]+", t):
        return "ain", f"{digits[:4]}-{digits[4:6]}-{digits[6]}-{digits[7:9]}-{digits[9:]}"
    if digits == t and len(digits) <= 9:
        return "pin", digits.zfill(9)
    raise ValueError(f"expected an Arapahoe AIN (2077-16-2-22-004) or PIN (034472207), got {text!r}")


# ----------------------------------------------------------- 1. county GIS layer

def epoch_ms_to_date(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()


def gis_observations(attrs: dict, snapshot_id: Optional[str], request_url: str,
                     record_url: str) -> list[Observation]:
    common = dict(source=GIS_SOURCE, url=request_url, snapshot_id=snapshot_id,
                  record_url=record_url)
    obs: list[Observation] = []
    for ours, theirs in GIS_FIELDS.items():
        value = clean(attrs.get(theirs))
        note = GIS_NOTES.get(ours)
        if ours in DATE_FIELDS:
            value = epoch_ms_to_date(value)
        if ours == "last_sale_price" and value == 0:
            note = "$0 transfer: not a market sale (e.g. quitclaim or related-party transfer)"
        if value is None:
            obs.append(Observation(field=ours, status="not_provided",
                                   note="blank in the county GIS layer", **common))
        else:
            obs.append(Observation(field=ours, value=value, note=note, **common))
    reception = clean(attrs.get(GIS_FIELDS["last_sale_reception"]))
    if reception:
        obs.append(Observation(field="last_sale_deed_url", value=DEED_SEARCH_URL.format(reception),
                               note="Clerk & Recorder search for the reception number", **common))
    owner = clean(attrs.get(GIS_FIELDS["owner"]))
    if owner:
        obs.append(Observation(field="owner_records_search_url",
                               value=DEED_SEARCH_URL.format(quote(owner)),
                               note="Clerk & Recorder search by owner name: deeds, deeds of trust, "
                                    "liens and any Statement of Authority (who may act for an "
                                    "entity owner). Read in recorded_documents, "
                                    "owner_acquisition_deed and statement_of_authority",
                               **common))
    return obs


# ------------------------------------------------------ 2. Assessor parcel page

def fetch_text(url: str, params: dict, source: str,
               client: Optional[httpx.Client] = None) -> tuple[str, Optional[str], str]:
    """GET one page with browser-like headers. Returns (text, snapshot_id, request_url).

    snapshot_id is None unless SAVE_RAW=1 (see resolve.save_snapshot).
    """
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    pages: list[dict] = []
    try:
        try:
            r = client.get(url, params=params, headers=HEADERS, follow_redirects=True)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
        pages.append({"url": str(r.request.url), "http_status": r.status_code, "body": r.text})
        if r.status_code != 200:
            raise SourceUnavailable(f"HTTP {r.status_code}")
    finally:
        snapshot_id = save_snapshot(params, pages, source)
        if own_client:
            client.close()
    return r.text, snapshot_id, str(r.request.url)


def fetch_page(ain: str, client: Optional[httpx.Client] = None) -> tuple[str, Optional[str], str]:
    """GET the parcel page. Returns (html, snapshot_id, request_url)."""
    return fetch_text(PAGE_URL, {"PPINum": ain}, PAGE_SOURCE, client)


def label(soup: BeautifulSoup, id_: str, sep: str = " ") -> Optional[str]:
    el = soup.find(id=id_)
    return clean(el.get_text(sep, strip=True)) if el else None


def to_number(value: Optional[str]) -> Any:
    """'4.00' -> 4.0, '1907' -> 1907; anything else is returned unchanged."""
    if value is None:
        return None
    s = value.replace(",", "")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return value


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def year_in(text: Optional[str]) -> Optional[str]:
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return m.group(0) if m else None


def is_hidden(row: Tag) -> bool:
    return bool(re.search(r"display\s*:\s*none", row.get("style", "")))


def parse_sales(soup: BeautifulSoup) -> list[dict]:
    """Sale rows: book/page (links to the Clerk's deed search), date, price, type."""
    title = soup.find(id="ucParcelSale_rptrSale_lblSaleTitle")
    if not title:
        return []
    sales = []
    for row in title.find_parent("table").find_all("tr")[1:]:
        tds = row.find_all("td", recursive=False)
        if len(tds) < 5:
            continue
        link = tds[1].find("a")
        flag = tds[1].find("span")
        raw_date = tds[2].get_text(strip=True)
        try:
            date = datetime.strptime(raw_date, "%m-%d-%Y").date().isoformat()
        except ValueError:
            date = clean(raw_date)
        sales.append({
            "book_page": clean(link.get_text(" ", strip=True).replace("\xa0", " ")) if link else None,
            "deed_search_url": link.get("href") if link else None,
            "date": date,
            "price": parse_money(tds[3].get_text(strip=True)),
            "type": clean(tds[4].get_text(" ", strip=True)),
            "flag": clean(flag.get_text(" ", strip=True)) if flag else None,
        })
    sales.sort(key=lambda s: s["date"] or "", reverse=True)
    return sales


def parse_buildings(soup: BeautifulSoup) -> list[dict]:
    """One dict per building. Rows the page hides (display:none) are skipped."""
    buildings: dict[int, dict] = {}
    for span in soup.find_all("span", id=re.compile(r"^ucParcelResdBuild_rptrResdBuild_\w+_\d+$")):
        idx = int(span["id"].rsplit("_", 1)[1])
        row = span.find_parent("tr")
        b = buildings.setdefault(idx, {"building": str(idx + 1)})
        tds = row.find_all("td", recursive=False)
        if len(tds) >= 2 and clean(tds[1].get_text(strip=True)):
            b["building"] = tds[1].get_text(strip=True)
        if is_hidden(row):
            continue
        value = clean(tds[-1].get_text(" ", strip=True)) if tds else None
        if value is not None:
            b[snake(span.get_text())] = to_number(value)
    return [buildings[i] for i in sorted(buildings)]


def parse_areas(soup: BeautifulSoup) -> list[dict]:
    """Area lines per building (first floor, basement, ...). Totals rows are skipped."""
    table = soup.find(id="tblAreasResidential")
    if not table:
        return []
    areas, building = [], None
    for row in table.find_all("tr"):
        tds = row.find_all("td", recursive=False)
        if len(tds) != 4 or "td_area" in (tds[0].get("class") or []):
            continue
        building = clean(tds[1].get_text(strip=True)) or building
        areas.append({"building": building,
                      "description": clean(tds[2].get_text(" ", strip=True)),
                      "sqft": to_number(clean(tds[3].get_text(strip=True)))})
    return areas


def parse_land_lines(soup: BeautifulSoup) -> list[dict]:
    th = next((t for t in soup.find_all("th") if t.get_text(strip=True) == "Land Line"), None)
    if not th:
        return []
    lines = []
    for row in th.find_parent("table").find_all("tr"):
        tds = row.find_all("td", recursive=False)
        if len(tds) != 4:
            continue
        m = re.fullmatch(r"([\d,.]+)\s*(\w+)", tds[1].get_text(strip=True))
        lines.append({"units": to_number(m.group(1)) if m else None,
                      "unit_type": m.group(2) if m else None,
                      "land_use": clean(tds[3].get_text(" ", strip=True))})
    return lines


def page_observations(html: str, snapshot_id: Optional[str],
                      request_url: str) -> list[Observation]:
    """Detail fields from the parcel page; see DETAIL_FIELDS."""
    soup = BeautifulSoup(html, "html.parser")
    common = dict(source=PAGE_SOURCE, url=request_url, snapshot_id=snapshot_id,
                  record_url=request_url)
    obs: list[Observation] = []

    def add(field: str, value: Any, note: Optional[str] = None, as_of: Optional[str] = None) -> None:
        if value is None or value == []:
            obs.append(Observation(field=field, status="not_provided",
                                   note=note or "blank on the Assessor's page", as_of=as_of, **common))
        else:
            obs.append(Observation(field=field, value=value, note=note, as_of=as_of, **common))

    for ours, id_ in HEADER_FIELDS.items():
        value = label(soup, f"ucParcelHeader_{id_}")
        add(ours, to_number(value) if ours in NUMBER_FIELDS else value)

    appeal = label(soup, "ucParcelHeader_lblAppealsLink")
    if appeal:
        add("appeal_notice", appeal)

    value_title = label(soup, "ucParcelValue_lblValueTitle")
    add("value_tax_year", year_in(value_title),
        f"tax year of the values on the page ({value_title})" if value_title else None)
    for title_id, fields in VALUE_GROUPS:
        title = label(soup, f"ucParcelValue_{title_id}")
        year = year_in(title)
        for ours, id_ in fields.items():
            raw = label(soup, f"ucParcelValue_{id_}")
            value = parse_money(raw)
            note = title
            if raw is not None and value is None:
                note = f"could not parse number from {raw!r}"
            add(ours, value, note, as_of=year)

    levy_title = label(soup, "ucParcelValue_lblLevyTitle")
    levy = label(soup, "ucParcelValue_lnkLevy")
    add("mill_levy", to_number(levy), levy_title.rstrip(":") if levy_title else None,
        as_of=year_in(levy_title))
    # the levy is a link to the per-authority breakdown (Levy.aspx?id=..&auth=..)
    levy_link = soup.find(id="ucParcelValue_lnkLevy")
    m = re.search(r"window\.open\('([^']+)'", (levy_link.get("onclick") or "") if levy_link else "")
    if m:
        obs[-1].record_url = str(httpx.URL(PAGE_URL).join(m[1]))

    sales = parse_sales(soup)
    add("sales", sales, "most recent first; date: the Assessor's sale date; book_page is the "
        "Clerk & Recorder reception" if sales else "no sales listed on the Assessor's page")

    add("buildings", parse_buildings(soup), "rows the county page hides are left out")
    areas = parse_areas(soup)
    add("building_areas", areas)
    living = [a for a in areas if a["sqft"] and LIVING_AREA.search(a["description"] or "")
              and not NOT_LIVING_AREA.search(a["description"] or "")]
    basement = [a for a in areas if a["sqft"] and BASEMENT.search(a["description"] or "")]
    if any(BASEMENT_TOTAL.search(a["description"] or "") for a in basement):
        basement = [a for a in basement if BASEMENT_TOTAL.search(a["description"] or "")]
    add("living_area_sqft", sum(a["sqft"] for a in living) if living else None,
        "above-grade living area: " + " + ".join(f"{a['description']} {a['sqft']}" for a in living)
        + "; basement, garage, patio and porches excluded" if living
        else "no above-grade floor lines on the Assessor's page")
    add("basement_sqft", sum(a["sqft"] for a in basement) if basement else None,
        "reported apart from the living area: " + ", ".join(
            f"{a['description']} {a['sqft']}" for a in basement) if basement
        else "no basement on the Assessor's page")

    land_lines = parse_land_lines(soup)
    add("land_lines", land_lines,
        "initial appraisal; does not reflect appeal adjustments (per the Assessor's page)")
    sqft = [ln["units"] for ln in land_lines if ln["unit_type"] == "SF" and ln["units"] is not None]
    acres = to_number(label(soup, f"ucParcelHeader_{HEADER_FIELDS['land_acres']}"))
    if land_lines and len(sqft) == len(land_lines):
        add("land_sqft", sum(sqft), "Assessor: sum of the land lines")
    elif isinstance(acres, (int, float)) and acres > 0:
        places = len(str(acres).split(".")[1]) if "." in str(acres) else 0
        add("land_sqft", round(acres * SQFT_PER_ACRE),
            f"Assessor: {acres} acres x {SQFT_PER_ACRE:,} (the land lines are not in square "
            f"feet); the acreage has {places} decimals, so +/- "
            f"{round(0.5 * 10 ** -places * SQFT_PER_ACRE):,} SF")
    else:
        add("land_sqft", None, "land is not measured in square feet on the Assessor's page"
            if land_lines else None)

    return obs


def fields_status(fields: list[str], status: str, note: str, source: str, url: str,
                  snapshot_id: Optional[str] = None) -> list[Observation]:
    """The same status for every field of a step that could not run or found nothing."""
    return [Observation(field=f, status=status, note=note, source=source, url=url,
                        snapshot_id=snapshot_id)
            for f in fields]


def detail_unavailable(status: str, note: str, url: Optional[str],
                       snapshot_id: Optional[str]) -> list[Observation]:
    return fields_status(DETAIL_FIELDS, status, note, PAGE_SOURCE, url or PAGE_URL, snapshot_id)


def assessor_page(ain: Optional[str], notes: list[str],
                  client: httpx.Client) -> list[Observation]:
    if not ain:
        notes.append("county GIS record has no AIN; parcel page not fetched")
        return detail_unavailable("not_provided", "no AIN to look up", None, None)
    page_url = str(httpx.URL(PAGE_URL, params={"PPINum": ain}))
    try:
        html, sid, req = fetch_page(ain, client)
    except SourceUnavailable as e:
        notes.append(f"{PAGE_SOURCE} unavailable: {e}")
        return detail_unavailable("source_unavailable", f"parcel page could not be read: {e}",
                                  page_url, None)
    if FOUND_MARKER in html:
        return page_observations(html, sid, req)
    if NOT_FOUND_TEXT in html:
        notes.append(f"the parcel page has no record for AIN {ain}, "
                     "although the county GIS layer does")
        return detail_unavailable("not_found", "the Assessor's page has no record for this AIN",
                                  req, sid)
    # a 200 that is neither a parcel nor the "no records" page: maintenance or a layout change
    notes.append("unexpected parcel page (maintenance or layout change)")
    return detail_unavailable("source_unavailable", "unexpected page (maintenance or layout change)",
                              req, sid)


# ------------------------------------------------------- shared by steps 3-6

def adder(obs: list[Observation], common: dict, blank_note: str) -> Callable[..., None]:
    """add(field, value, note, as_of): ok when the value is set, otherwise not_provided."""
    def add(field: str, value: Any, note: Optional[str] = None, as_of: Optional[str] = None) -> None:
        if value is None or value == []:
            obs.append(Observation(field=field, status="not_provided", note=note or blank_note,
                                   as_of=as_of, **common))
        else:
            obs.append(Observation(field=field, value=value, note=note, as_of=as_of, **common))
    return add


def distinct(rows: list[dict], field: str) -> Any:
    """The one value of `field` across rows, a list when they differ, None when blank."""
    values: list = []
    for row in rows:
        v = clean(row.get(field))
        if v is not None and v not in values:
            values.append(v)
    return values[0] if len(values) == 1 else (values or None)


def layer_rows(url: str, params: dict, source: str, client: httpx.Client,
               post: bool = False) -> tuple[list[dict], Optional[str], str]:
    data, sid, req = query_layer(params, client, url=url, source=source, post=post)
    return [f["attributes"] for f in data["features"]], sid, req


# ------------------------------------------------------- 3. value history (GIS)

def value_history(ain: Optional[str], current: Optional[Observation], tax_year: Optional[str],
                  client: httpx.Client) -> list[Observation]:
    """Appraised (actual) value per tax year, newest first.

    Field appraised_total_history, one Observation per year with the year in
    as_of. `current` (the GIS layer's appraised_total) is added under
    `tax_year`, the year the Assessor's page gives its values.
    """
    field = "appraised_total_history"
    if not ain:
        return fields_status([field], "not_provided", "no AIN to look up", HISTORY_SOURCE,
                             CAMA_URL.format(75))
    by_year: dict[str, Observation] = {}
    if current is not None and tax_year:
        by_year[tax_year] = current.model_copy(update={
            "field": field, "as_of": tax_year,
            "note": "current value in the county GIS layer; tax year from the Assessor's page"})
    errors = []
    for layer, fields in VALUE_CHANGE_LAYERS.items():
        try:
            rows, sid, url = layer_rows(
                CAMA_URL.format(layer),
                {"where": f"PARCEL_ID = '{sql_quote(ain)}'", "returnGeometry": "false"},
                HISTORY_SOURCE, client)
        except SourceUnavailable as e:
            errors.append(f"layer {layer}: {e}")
            continue
        for year, theirs in fields.items():
            value = distinct(rows, theirs)
            # 0 is how these layers leave a year blank
            if str(year) in by_year or value in (None, 0):
                continue
            if isinstance(value, float) and value.is_integer():
                value = int(value)            # some layers store whole dollars as doubles
            note = f"{theirs} in CustomCAMA_WM layer {layer}"
            if isinstance(value, list):
                note += f"; its {len(rows)} records for this AIN disagree"
            by_year[str(year)] = Observation(field=field, value=value, as_of=str(year),
                                             source=HISTORY_SOURCE, url=url, snapshot_id=sid,
                                             note=note)
    obs = [by_year[y] for y in sorted(by_year, reverse=True)]
    if errors:
        obs += fields_status([field], "source_unavailable", "; ".join(errors), HISTORY_SOURCE,
                             CAMA_URL.format(75))
    elif len(obs) == (1 if current is not None and tax_year else 0):
        obs.append(Observation(field=field, status="not_found", source=HISTORY_SOURCE,
                               url=CAMA_URL.format(75),
                               note="no earlier values for this AIN in the value-change layers"))
    return obs


# ------------------------------------------------------------- 4. Treasurer

def treasurer_observations(pin: Optional[str], ain: Optional[str],
                           client: httpx.Client) -> list[Observation]:
    """Current tax year from ReReport.aspx, then the prior year from PrevYear.aspx."""
    if not pin:
        return fields_status(TAX_FIELDS + PRIOR_TAX_FIELDS, "not_provided", "no PIN to look up",
                             TREASURER_SOURCE, TREASURER_URL)
    try:
        html, sid, url = fetch_text(TREASURER_URL, {"PIN": pin}, TREASURER_SOURCE, client)
    except SourceUnavailable as e:
        return fields_status(TAX_FIELDS + PRIOR_TAX_FIELDS, "source_unavailable",
                             f"tax page could not be read: {e}", TREASURER_SOURCE, TREASURER_URL)
    if TREASURER_FOUND not in html:
        status, note = (("not_found", "the Treasurer has no record for this PIN")
                        if TREASURER_NOT_FOUND in html else
                        ("source_unavailable", "unexpected tax page (maintenance or layout change)"))
        return fields_status(TAX_FIELDS + PRIOR_TAX_FIELDS, status, note, TREASURER_SOURCE, url, sid)

    soup = BeautifulSoup(html, "html.parser")

    def lab(id_: str) -> Optional[str]:
        return label(soup, f"ContentPlaceHolder1_{id_}")

    m = re.search(r"Last updated on (\d{2})/(\d{2})/(\d{4})", lab("lblUpdateDate") or "")
    updated = f"{m[3]}-{m[1]}-{m[2]}" if m else None
    obs: list[Observation] = []
    add = adder(obs, dict(source=TREASURER_SOURCE, url=url, snapshot_id=sid, record_url=url),
                "blank on the Treasurer's page")
    years = re.search(r"(\d{4}) Payable (\d{4})", lab("lblPayable") or "")
    add("tax_year", years[1] if years else None,
        f"taxes for {years[1]} payable {years[2]}" if years else None, as_of=updated)
    add("tax_rate", to_number(lab("lblMilllevy")), "total tax rate (mill levy / 1000)",
        as_of=updated)
    for ours, id_ in TAX_MONEY_FIELDS.items():
        add(ours, parse_money(lab(id_)), TREASURER_NOTE if ours == "tax_due" else None,
            as_of=updated)
    for ours, id_ in TAX_FLAGS.items():
        raw = lab(id_)
        add(ours, {"Y": True, "N": False}.get(raw or "", raw), as_of=updated)
    add("tax_bill_pdf_url", TAX_BILL_URL.format(ain or lab("lblAIN"), pin),
        "Treasurer's tax statement (PDF) for the current tax year; not fetched")
    return obs + prior_year_tax(pin, client)


def prior_year_tax(pin: str, client: httpx.Client) -> list[Observation]:
    try:
        html, sid, url = fetch_text(PRIOR_YEAR_URL, {"PIN": pin}, TREASURER_SOURCE, client)
    except SourceUnavailable as e:
        return fields_status(PRIOR_TAX_FIELDS, "source_unavailable",
                             f"prior-year page could not be read: {e}", TREASURER_SOURCE,
                             PRIOR_YEAR_URL)
    soup = BeautifulSoup(html, "html.parser")

    def lab(id_: str) -> Optional[str]:
        return label(soup, f"ContentPlaceHolder1_{id_}")

    years = re.search(r"(\d{4}) Payable (\d{4})", lab("lblPayable") or "")
    year = years[1] if years else None
    obs: list[Observation] = []
    add = adder(obs, dict(source=TREASURER_SOURCE, url=url, snapshot_id=sid, record_url=url),
                "blank on the Treasurer's prior-year page")
    add("tax_prior_year", year, f"taxes for {year} payable {years[2]}" if years else None)
    add("tax_prior_year_rate", to_number(lab("lblTaxRate")), "total tax rate", as_of=year)
    for ours, id_ in PRIOR_MONEY_FIELDS.items():
        add(ours, parse_money(lab(id_)), "only amounts received and applied by December 31 "
            "of the payable year (per the Treasurer's page)", as_of=year)
    return obs


# --------------------------------------------------------------- 5. location

def point_params(x: float, y: float) -> dict:
    return {"geometry": f"{x},{y}", "geometryType": "esriGeometryPoint", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false"}


def location_observations(attrs: dict, rings: Optional[list],
                          client: httpx.Client) -> list[Observation]:
    """Jurisdiction, zoning and flood at the county's point inside the parcel;
    flood_sfha_on_parcel checks the whole parcel polygon."""
    x, y = attrs.get(GIS_FIELDS["longitude"]), attrs.get(GIS_FIELDS["latitude"])
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return fields_status(["jurisdiction", *ZONING_FIELDS, *FLOOD_FIELDS,
                              "flood_sfha_on_parcel", *FLOOD_PANEL_FIELDS],
                             "not_provided", "the county GIS record has no point for the parcel",
                             LOCATION_SOURCE, JURISDICTION_URL)
    point = point_params(x, y)
    return (jurisdiction_and_zoning(point, client) + flood_observations(point, client)
            + sfha_on_parcel(rings, client) + flood_panel(point, client))


def jurisdiction_and_zoning(point: dict, client: httpx.Client) -> list[Observation]:
    try:
        rows, sid, url = layer_rows(JURISDICTION_URL, point, LOCATION_SOURCE, client)
    except SourceUnavailable as e:
        return fields_status(["jurisdiction", *ZONING_FIELDS], "source_unavailable",
                             f"jurisdiction layer: {e}", LOCATION_SOURCE, JURISDICTION_URL)
    obs: list[Observation] = []
    add = adder(obs, dict(source=LOCATION_SOURCE, url=url, snapshot_id=sid),
                "no jurisdiction at the parcel's point")
    jurisdiction = distinct(rows, "JURISDICTION")
    add("jurisdiction", jurisdiction, POINT_NOTE)
    if jurisdiction == "LITTLETON":
        obs += zoning(LITTLETON_ZONING_URL, LITTLETON_SOURCE, point,
                      {"zoning_code": "ZONECLASS", "zoning_desc": "ZONEDESC",
                       "zoning_code_url": "BASE_DIST_DESC"}, client)
    elif jurisdiction == "UNINCORPORATED":
        obs += zoning(COUNTY_ZONING_URL, LOCATION_SOURCE, point,
                      {"zoning_code": "ZONING", "zoning_code_url": "HYPERLINK",
                       "zoning_case": "CASE_NUMBER", "zoning_case_doc_url": "Zoning_Doc"}, client)
    else:
        obs += fields_status(ZONING_FIELDS, "not_provided",
                             f"zoning is set by {jurisdiction or 'the municipality'}, "
                             "whose map is not queried yet", LOCATION_SOURCE, url, sid)
    return obs


def zoning(url: str, source: str, point: dict, fields: dict[str, str],
           client: httpx.Client) -> list[Observation]:
    try:
        rows, sid, req = layer_rows(url, point, source, client)
    except SourceUnavailable as e:
        return fields_status(list(fields), "source_unavailable", f"zoning layer: {e}", source, url)
    obs: list[Observation] = []
    add = adder(obs, dict(source=source, url=req, snapshot_id=sid),
                "no zoning district at the parcel's point")
    for ours, theirs in fields.items():
        add(ours, distinct(rows, theirs), POINT_NOTE)
    return obs


def flood_observations(point: dict, client: httpx.Client) -> list[Observation]:
    try:
        rows, sid, url = layer_rows(FLOOD_URL, point, LOCATION_SOURCE, client)
    except SourceUnavailable as e:
        return fields_status(FLOOD_FIELDS, "source_unavailable", f"flood layer: {e}",
                             LOCATION_SOURCE, FLOOD_URL)
    obs: list[Observation] = []
    add = adder(obs, dict(source=LOCATION_SOURCE, url=url, snapshot_id=sid),
                "no FEMA flood area mapped at the parcel's point")
    sfha = distinct(rows, "SFHA_TF")
    bfe = distinct([r for r in rows if r.get("STATIC_BFE") != NO_BFE], "STATIC_BFE")
    add("flood_zone", distinct(rows, "FLD_ZONE"), f"FEMA flood zone {POINT_NOTE}")
    add("flood_zone_subtype", distinct(rows, "ZONE_SUBTY"), POINT_NOTE)
    add("flood_sfha", {"T": True, "F": False}.get(sfha, sfha) if rows else None,
        f"in FEMA's Special Flood Hazard Area (high risk), {POINT_NOTE}")
    add("flood_bfe", bfe, f"static base flood elevation, {POINT_NOTE}" if bfe is not None
        else "no static base flood elevation at the parcel's point")
    return obs


def sfha_on_parcel(rings: Optional[list], client: httpx.Client) -> list[Observation]:
    """True when any Special Flood Hazard Area intersects the parcel polygon.

    The polygon is sent by POST: it can be too long for a URL.
    """
    field = "flood_sfha_on_parcel"
    if not rings:
        return fields_status([field], "not_provided", "no parcel polygon in the county GIS record",
                             LOCATION_SOURCE, FLOOD_URL)
    params = {"geometry": json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}}),
              "geometryType": "esriGeometryPolygon", "inSR": "4326",
              "spatialRel": "esriSpatialRelIntersects", "where": "SFHA_TF = 'T'",
              "returnGeometry": "false"}
    try:
        rows, sid, url = layer_rows(FLOOD_URL, params, LOCATION_SOURCE, client, post=True)
    except SourceUnavailable as e:
        return fields_status([field], "source_unavailable", f"flood layer: {e}",
                             LOCATION_SOURCE, FLOOD_URL)
    zones = distinct(rows, "FLD_ZONE")
    note = (f"zone(s) {zones} touch or cross the parcel polygon; check the panel for how much"
            if rows else "no Special Flood Hazard Area touches the parcel polygon")
    return [Observation(field=field, value=bool(rows), note=note, source=LOCATION_SOURCE,
                        url=url, snapshot_id=sid)]


def flood_panel(point: dict, client: httpx.Client) -> list[Observation]:
    try:
        rows, sid, url = layer_rows(FLOOD_PANEL_URL, point, LOCATION_SOURCE, client)
    except SourceUnavailable as e:
        return fields_status(FLOOD_PANEL_FIELDS, "source_unavailable", f"flood panel layer: {e}",
                             LOCATION_SOURCE, FLOOD_PANEL_URL)
    obs: list[Observation] = []
    add = adder(obs, dict(source=LOCATION_SOURCE, url=url, snapshot_id=sid),
                "no FEMA flood map panel at the parcel's point")
    add("flood_panel", distinct(rows, "FIRM_PAN"), f"FEMA flood map (FIRM) panel {POINT_NOTE}")
    add("flood_panel_url", distinct(rows, "HYPERLINK"), "the county's copy of the panel PDF")
    return obs


# ------------------------------------------------ derived from the steps above

def first_ok(obs: list[Observation], field: str) -> Optional[Observation]:
    return next((o for o in obs if o.field == field and o.status == "ok"), None)


def owner_parties(obs: list[Observation]) -> list[Observation]:
    """`owners`: the owner of record split into its parties (people as First Last),
    with the owner type and the Assessor's ownership type (e.g. joint tenancy)."""
    owner, tenancy = first_ok(obs, "owner"), first_ok(obs, "ownership_type")
    if not owner:
        return []
    split = split_owner(owner.value)
    value = {"type": split["type"], "tenancy": tenancy.value if tenancy else None,
             "parties": split["parties"]}
    note = (f"split from the owner of record {owner.value!r} (the county writes people as "
            f"LAST FIRST)" if split["type"] in ("individual", "individuals")
            else f"one {split['type']}; a business or trust name is not split")
    if tenancy:
        note += f"; tenancy: the Assessor's ownership type ({tenancy.value})"
    return [owner.model_copy(update={"field": "owners", "value": value, "note": note})]


def absentee_owner(obs: list[Observation]) -> list[Observation]:
    """owner_absentee: false when the owner's mailing address is the site address."""
    def v(field: str) -> Optional[str]:
        o = first_ok(obs, field)
        return str(o.value) if o and o.value is not None else None
    street, site = v("owner_mail_street"), v("site_address")
    src = first_ok(obs, "owner_mail_street") or first_ok(obs, "owner")
    if not src:
        return []
    if not street or not site:
        return [src.model_copy(update={"field": "owner_absentee", "value": None,
                                       "status": "not_provided",
                                       "note": "no mailing or site address to compare"})]
    a, b = normalize_address(street), normalize_address(site)
    zip_a, zip_b = (v("owner_mail_zip") or "")[:5], (v("site_zip") or "")[:5]
    same = all(a[k] == b[k] for k in ("number", "predir", "name", "unit")) and \
        (not zip_a or not zip_b or zip_a == zip_b)
    mail = ", ".join(x for x in (street, v("owner_mail_city"), v("owner_mail_zip")) if x)
    return [src.model_copy(update={
        "field": "owner_absentee", "value": not same,
        "note": (f"the mailing address ({mail}) is the site address: owner-occupied or at "
                 f"least mailed at the property" if same else
                 f"the mailing address ({mail}) is not the site address ({site})")})]


def jurisdiction_note(obs: list[Observation]) -> None:
    """The postal city is not the jurisdiction: an Aurora ZIP can be unincorporated."""
    j, city = first_ok(obs, "jurisdiction"), first_ok(obs, "site_city")
    if j and city and str(city.value).upper() != str(j.value).upper():
        where = ("unincorporated Arapahoe County" if j.value == "UNINCORPORATED"
                 else f"the jurisdiction {j.value}")
        j.note = (j.note or "") + (f". The postal city ({city.value}) is not the jurisdiction: the parcel is in "
                   f"{where}, which sets its zoning and issues its permits")


def sale_dates(obs: list[Observation]) -> None:
    """Label the two last-sale dates: the GIS date against the Clerk's recording
    date, and the Assessor's sale date (often the deed's date, before recording)."""
    gis, deed, sales = (first_ok(obs, "last_sale_date"), first_ok(obs, "owner_acquisition_deed"),
                        first_ok(obs, "sales"))
    recorded = deed.value.get("recorded") if deed else None
    reception = deed.value.get("instrument") if deed else None
    if gis and recorded:
        gis.note = ("recording date: the same as the Clerk & Recorder's recorded date of "
                    f"{reception}" if gis.value == recorded else
                    f"the county GIS sale date; the Clerk & Recorder recorded {reception} on "
                    f"{recorded}")
    if sales and reception:
        for s in sales.value:
            if (s.get("book_page") or "").replace(" ", "") == reception:
                s["recorded"] = recorded
                if s.get("date") and recorded and s["date"] != recorded:
                    sales.note = (sales.note or "") + (f". For {reception}, date ({s['date']}) is the Assessor's sale "
                                   f"date and recorded ({recorded}) the Clerk & Recorder's "
                                   f"recording date: a deed is usually signed before it is "
                                   f"recorded")


def land_area_note(obs: list[Observation]) -> None:
    """land_sqft (Assessor) is primary; the GIS polygon area is secondary."""
    land, gis = first_ok(obs, "land_sqft"), first_ok(obs, "gis_area_sqft")
    if land and gis and land.value:
        diff = (gis.value - land.value) / land.value * 100
        land.note = (land.note or "") + (f". Primary figure; the GIS polygon (gis_area_sqft) is "
                      f"{round(gis.value):,} SF, {diff:+.1f}%")
        gis.note = (gis.note or "") + f"; secondary to land_sqft ({land.value:,} SF, the Assessor's)"


def permits_for(jurisdiction: Optional[Observation], site: Optional[str], ain: Optional[str],
                client: httpx.Client) -> list[Observation]:
    """Permits from whoever issues them: the City of Littleton or the county."""
    j = jurisdiction.value if jurisdiction else None
    if j == "LITTLETON":
        return littleton_permits(site, ain, client)
    if j == "UNINCORPORATED":
        return county_permits(site, client)
    issuer = PERMIT_ISSUERS.get(j or "", f"the City of {str(j).title()}" if j
                                else "the jurisdiction")
    return [Observation(field="permits", status="not_provided", source=LOCATION_SOURCE,
                        url=JURISDICTION_URL,
                        note=f"permits are issued by {issuer}, whose permit portal is not "
                             f"queried yet")]


# ------------------------------------------------------------------------ lookup

def lookup(key: str, client: Optional[httpx.Client] = None, detail: bool = True) -> SourceRecord:
    """Look up one Arapahoe parcel by AIN (the statewide parcel_id) or PIN.

    Raises ValueError if `key` is neither. With detail=False only the county
    GIS layer is queried (one JSON request, no HTML).
    """
    kind, value = normalize_key(key)
    if client is not None:
        return _lookup(key, kind, value, client, detail)
    with httpx.Client(timeout=30) as own_client:
        return _lookup(key, kind, value, own_client, detail)


def _lookup(key: str, kind: str, value: str, client: httpx.Client, detail: bool) -> SourceRecord:
    record = SourceRecord(source=SOURCE, input=key, status="not_found")
    if value != key.strip():
        record.notes.append(f"searched as {kind.upper()} {value!r}")

    # 1. county GIS layer (with the polygon, for the flood check in step 5)
    q = sql_quote(value)
    where = f"{GIS_FIELDS['parcel_id']} = '{q}' OR {GIS_FIELDS['account']} = '{q}'"
    try:
        data, sid, url = query_layer({"where": where, "returnGeometry": "true"}, client,
                                     url=GIS_URL, source=GIS_SOURCE, page_size=GIS_PAGE_SIZE)
    except SourceUnavailable as e:
        record.status = "source_unavailable"
        record.notes.append(f"{GIS_SOURCE} unavailable: {e}")
        return record
    record.url, record.snapshot_id = url, sid
    features = data["features"]
    if not features:
        record.notes.append(f"no parcel with this {kind.upper()} in the county GIS layer")
        return record
    if len(features) > 1:
        record.status = "ambiguous"
        ids = sorted({clean(f["attributes"].get(GIS_FIELDS["parcel_id"])) or "?" for f in features})
        record.notes.append(f"{len(features)} records match in the county GIS layer: {ids}")
        return record

    attrs = features[0]["attributes"]
    ain = clean(attrs.get(GIS_FIELDS["parcel_id"]))
    page_url = str(httpx.URL(PAGE_URL, params={"PPINum": ain})) if ain else None
    record.status = "ok"
    record.observations = gis_observations(attrs, sid, url, page_url or PAGE_URL)
    if not detail:
        return record

    # 2. Assessor parcel page, for what the layer lacks
    record.observations += assessor_page(ain, record.notes, client)

    def found(field: str) -> Optional[Observation]:
        return next((o for o in record.observations if o.field == field and o.status == "ok"), None)

    # 3-9. one Observation list per step; each step reports its own failures
    tax_year = found("value_tax_year")
    record.observations += value_history(ain, found("appraised_total"),
                                         tax_year.value if tax_year else None, client)
    record.observations += treasurer_observations(clean(attrs.get(GIS_FIELDS["account"])), ain,
                                                  client)
    record.observations += tax_check(record.observations, client)
    rings = (features[0].get("geometry") or {}).get("rings")
    record.observations += location_observations(attrs, rings, client)
    jurisdiction_note(record.observations)
    record.observations += owner_parties(record.observations)
    record.observations += absentee_owner(record.observations)
    owner = clean(attrs.get(GIS_FIELDS["owner"]))
    if owner:
        entity = sos_lookup(owner, client)
        record.observations += entity.observations
        record.notes += [f"owner entity: {n}" for n in entity.notes]
        # 7. Clerk & Recorder: documents naming the owner(s), the deed to them, an
        # entity's Statement of Authority
        legal = found("legal_description")
        record.observations += recorder_observations(
            owner, clean(attrs.get(GIS_FIELDS["last_sale_reception"])),
            legal.value if legal else None, client)
    sale_dates(record.observations)
    land_area_note(record.observations)

    # 8. permits, from whoever issues them
    site = clean(attrs.get(GIS_FIELDS["site_address"]))
    record.observations += permits_for(found("jurisdiction"), site, ain, client)

    # 9. businesses registered at the site address (possible occupants)
    entity_obs = found("owner_entity")
    record.observations += site_entities(
        site, clean(attrs.get(GIS_FIELDS["site_zip"])), client,
        exclude=entity_obs.value.get("entity_id") if entity_obs and isinstance(entity_obs.value,
                                                                               dict) else None)

    # 10. contact routes, from the observations above (nothing fetched)
    address_owner_note(record.observations)
    record.observations += contact_routes(record.observations)

    down = sorted({o.source for o in record.observations
                   if o.status == "source_unavailable" and o.source != PAGE_SOURCE})
    record.notes += [f"{s} unavailable for some fields; see their observations" for s in down]
    record.notes += [f"{o.field}: {o.note}" for o in record.observations
                     if o.status == "check_failed"]
    return record


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--no-detail"]
    if not args:
        sys.exit('usage: python -m core.colorado.arapahoe.arapahoe "<AIN, PIN or address>" [--no-detail]')
    text = " ".join(args)
    if looks_like_apn(text):
        key = text
    else:
        res = resolve(text)
        if res.status != "match" or res.parcel is None:
            payload = res.model_dump_json(indent=2)
            print(payload)
            path = save_result(RESULTS_SUBDIR, text, payload, RESULT_TIME_FORMAT)
            sys.exit(f"statewide resolve returned {res.status}; pass an AIN or PIN instead\n"
                     f"saved to {path}")
        if res.parcel.county != COUNTY:
            sys.exit(f"parcel is in {res.parcel.county} County, not {COUNTY}")
        key = res.parcel.parcel_id or ""
    try:
        rec = lookup(key, detail="--no-detail" not in sys.argv)
    except ValueError as e:
        sys.exit(str(e))
    payload = rec.model_dump_json(indent=2)
    print(payload)
    # stderr, so piping the JSON (e.g. into jq) stays clean
    print(f"saved to {save_result(RESULTS_SUBDIR, text, payload, RESULT_TIME_FORMAT)}",
          file=sys.stderr)
