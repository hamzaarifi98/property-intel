"""City of Littleton building permits (eTRAKiT).

Searches permits by site address, then opens each one (a row click posts the
form back) for its dates, scope, APN and inspections. The public view of
eTRAKiT hides each permit's contacts (applicant, contractor, owner).

    permits("5241 S RIO GRANDE ST", "2077-16-2-22-004", client)
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from models import Observation
from resolve import SourceUnavailable, clean, iso_date, normalize_address

from .settings import (PERMITS_URL, PERMITS_SOURCE, HEADERS, SEARCH_BY, SEARCH_OPER, SEARCH_TEXT,
                       SEARCH_BUTTON, RESULTS_GRID, RESULTS_TABLE_ID, BY_ADDRESS, BEGINS_WITH,
                       MAX_DETAILS, PERMIT_LABELS, DATE_LABELS, BUSINESS_LABEL, APN_LABEL,
                       PERMITS_NOTE)


def post(client: httpx.Client, data: Optional[dict] = None) -> BeautifulSoup:
    try:
        r = (client.post(PERMITS_URL, data=data, headers=HEADERS, follow_redirects=True)
             if data is not None else
             client.get(PERMITS_URL, headers=HEADERS, follow_redirects=True))
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise SourceUnavailable(f"HTTP {r.status_code}")
    return BeautifulSoup(r.text, "html.parser")


def form_state(soup: BeautifulSoup) -> dict:
    """The form's fields as the browser would post them (no buttons)."""
    data = {i["name"]: i.get("value", "") for i in soup.select("input[name]")
            if i.get("type") not in ("submit", "button", "image", "checkbox")}
    for s in soup.select("select[name]"):
        opt = s.find("option", selected=True) or s.find("option")
        data[s["name"]] = opt.get("value", "") if opt else ""
    return data


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


def permits(site_address: Optional[str], apn: Optional[str],
            client: httpx.Client) -> list[Observation]:
    """Permits at one site address, newest first. `apn` marks permits filed under
    another parcel number (the same address can span parcels)."""
    field = "permits"
    want = normalize_address(site_address or "")
    if not want["number"] or not want["name"]:
        return [Observation(field=field, status="not_provided", source=PERMITS_SOURCE,
                            url=PERMITS_URL, note=f"no street address to search ({site_address!r})")]
    # "5241 S RIO GRANDE" without the street type: permits write ST, St or STREET
    text = " ".join(p for p in (want["number"], want["predir"], want["name"]) if p)
    try:
        form = form_state(post(client))
        form.update({SEARCH_BY: BY_ADDRESS, SEARCH_OPER: BEGINS_WITH, SEARCH_TEXT: text,
                     "__EVENTTARGET": SEARCH_BUTTON, "__EVENTARGUMENT": ""})
        results = post(client, form)
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
                page = post(client, {**state, "__EVENTTARGET": RESULTS_GRID,
                                     "__EVENTARGUMENT": f"RowClick;{i}"})
                items.append({**item, **detail(page)})
            except SourceUnavailable as e:
                failed.append(f"{item['permit_no']}: {e}")
                items.append(item)
    except SourceUnavailable as e:
        return [Observation(field=field, status="source_unavailable", source=PERMITS_SOURCE,
                            url=PERMITS_URL, note=f"permit search: {e}")]

    common = dict(source=PERMITS_SOURCE, url=PERMITS_URL, record_url=PERMITS_URL)
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
