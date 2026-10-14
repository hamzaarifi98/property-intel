"""eTRAKiT (CentralSquare) permit portals, e.g. permit9.littletonco.gov/etrakit.

An ASP.NET WebForms page: the search and each permit's detail are form posts
that carry the page's __VIEWSTATE. Searches permits by site address, then opens
each one (a row click posts the form back) for its dates, scope, APN and
inspections. The public view hides each permit's contacts (applicant,
contractor, owner). Config (`permits`): url (the Search/permit.aspx page), source.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from core.context import Context
from core.platforms.web import HEADERS, form_state
from core.models import Observation
from core.common import SourceUnavailable, clean, iso_date, normalize_address

# Form controls (name attributes) of eTRAKiT's permit search
SEARCH_BY = "ctl00$cplMain$ddSearchBy"
SEARCH_OPER = "ctl00$cplMain$ddSearchOper"
SEARCH_TEXT = "ctl00$cplMain$txtSearchString"
SEARCH_BUTTON = "ctl00$cplMain$btnSearch"
RESULTS_GRID = "ctl00$cplMain$rgSearchRslts"          # a row click posts "RowClick;<index>"
RESULTS_TABLE_ID = "ctl00_cplMain_rgSearchRslts_ctl00"
BY_ADDRESS = "Permit_Main.SITE_ADDR"
BEGINS_WITH = "BEGINS WITH"
MAX_DETAILS = 40                                      # permit pages read per lookup, at most
# Permit Info labels: our field -> label id suffix (cplMain_ctlNN_<id>)
PERMIT_LABELS = {
    "type": "lblPermitType", "subtype": "lblPermitSubtype", "scope": "lblPermitDesc",
    "status": "lblPermitStatus", "applied": "lblPermitAppliedDate",
    "approved": "lblPermitApprovedDate", "issued": "lblPermitIssuedDate",
    "finaled": "lblPermitFinaledDate", "expires": "lblPermitExpirationDate",
    "description": "lblPermitNotes",
}
DATE_LABELS = {"applied", "approved", "issued", "finaled", "expires"}
BUSINESS_LABEL = "lblSiteTract"                       # shown as "Business Name:"
APN_LABEL = "lblParcelNumberLbl"                      # the value is in the next column
PERMITS_NOTE = ("building, right-of-way and other permits at the site address, newest first. "
                "Contacts (applicant, contractor, owner) are not shown to public users of "
                "eTRAKiT, so no contractor is named here")


def post(url: str, client: httpx.Client, data: Optional[dict] = None) -> BeautifulSoup:
    try:
        r = (client.post(url, data=data, headers=HEADERS, follow_redirects=True)
             if data is not None else
             client.get(url, headers=HEADERS, follow_redirects=True))
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise SourceUnavailable(f"HTTP {r.status_code}")
    return BeautifulSoup(r.text, "html.parser")


def label(soup: BeautifulSoup, suffix: str) -> Optional[str]:
    el = soup.find(id=re.compile(rf"^cplMain_ctl\d+_{suffix}$"))
    return clean(re.sub(r"\s+", " ", el.get_text(" ", strip=True))) if el else None


def detail(soup: BeautifulSoup) -> dict:
    """Permit Info, Site Info and inspections of one opened permit."""
    item = {}
    for ours, suffix in PERMIT_LABELS.items():
        value = label(soup, suffix)
        item[ours] = iso_date(value) if ours in DATE_LABELS else value
    apn_label = soup.find(id=re.compile(rf"^cplMain_ctl\d+_{APN_LABEL}$"))
    apn_cell = apn_label.find_parent("div").find_next_sibling("div") if apn_label else None
    item["apn"] = clean(apn_cell.get_text(" ", strip=True)) if apn_cell else None
    item["business_name"] = label(soup, BUSINESS_LABEL)
    inspections = []
    table = soup.find(id=re.compile(r"rgInspectionInfo_ctl00$"))
    for tr in table.find_all("tr") if table else []:
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(tds) >= 5 and tds[0]:
            inspections.append({"type": tds[0], "result": clean(tds[1]),
                                "date": iso_date(tds[4] or tds[2])})
    inspections.sort(key=lambda i: i["date"] or "", reverse=True)
    item["inspections"] = len(inspections)
    item["last_inspection"] = inspections[0] if inspections else None
    return item


def same_site(a: str, b: str) -> bool:
    x, y = normalize_address(a), normalize_address(b)
    return x["number"] == y["number"] and x["name"] == y["name"] and x["unit"] == y["unit"]


def run(cfg: dict, ctx: Context) -> None:
    ctx.add(permits(cfg, ctx.value("site_address"), ctx.ids.get("parcel_id"), ctx.client))


def permits(cfg: dict, site_address: Optional[str], apn: Optional[str],
            client: httpx.Client) -> list[Observation]:
    """Permits at one site address, newest first. `apn` marks permits filed under
    another parcel number (the same address can span parcels)."""
    field = "permits"
    url, source = cfg["url"], cfg["source"]
    want = normalize_address(site_address or "")
    if not want["number"] or not want["name"]:
        return [Observation(field=field, status="not_provided", source=source,
                            url=url, note=f"no street address to search ({site_address!r})")]
    # "5241 S RIO GRANDE" without the street type: permits write ST, St or STREET
    text = " ".join(p for p in (want["number"], want["predir"], want["name"]) if p)
    try:
        form = form_state(post(url, client))
        form.update({SEARCH_BY: BY_ADDRESS, SEARCH_OPER: BEGINS_WITH, SEARCH_TEXT: text,
                     "__EVENTTARGET": SEARCH_BUTTON, "__EVENTARGUMENT": ""})
        results = post(url, client, form)
        table = results.find(id=RESULTS_TABLE_ID)
        if table is None:
            raise SourceUnavailable("no results table (layout change?)")
        rows = []
        for i, tr in enumerate(table.select("tr.rgRow, tr.rgAltRow")):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
            if len(tds) >= 4 and same_site(tds[3], site_address or ""):
                rows.append((i, {"permit_no": tds[0], "record_id": clean(tds[5]) if len(tds) > 5
                                 else None}))
        paged = results.select_one(".rgPager") is not None
        state = form_state(results)
        items, failed = [], []
        for i, item in rows[:MAX_DETAILS]:
            try:
                page = post(url, client, {**state, "__EVENTTARGET": RESULTS_GRID,
                                     "__EVENTARGUMENT": f"RowClick;{i}"})
                items.append({**item, **detail(page)})
            except SourceUnavailable as e:
                failed.append(f"{item['permit_no']}: {e}")
                items.append(item)
    except SourceUnavailable as e:
        return [Observation(field=field, status="source_unavailable", source=source,
                            url=url, note=f"permit search: {e}")]

    common = dict(source=source, url=url, record_url=url)
    searched = f"{BEGINS_WITH.lower()} {text!r}"
    if not items:
        return [Observation(field=field, status="not_found", **common,
                            note=f"no permit at this address (address {searched})")]
    items.sort(key=lambda p: p.get("applied") or p.get("issued") or "", reverse=True)
    extra = [f"searched: address {searched}"]
    other = sorted({p["apn"] for p in items if p.get("apn") and apn and p["apn"] != apn})
    if other:
        extra.append(f"some permits are filed under another APN: {other}")
    if len(rows) > MAX_DETAILS:
        extra.append(f"only the first {MAX_DETAILS} of {len(rows)} permits were opened")
    if paged:
        extra.append("the result list has more pages; only the first was read")
    if failed:
        extra.append(f"could not open: {failed}")
    return [Observation(field=field, value=items, note=f"{PERMITS_NOTE}. {'; '.join(extra)}",
                        **common)]
