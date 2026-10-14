"""Arapahoe County permits (unincorporated land): Accela Citizen Access.

The Building module and each record's page need a login; the site-wide search
does not. It is searched for "<number> <direction> <street>" and its record
list read: date, record number, project name, type, status.

    county_permits("18949 E GARDEN DR", client)
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from models import Observation
from resolve import clean, iso_date, normalize_address

from .settings import (HEADERS, COUNTY_PERMITS_URL, COUNTY_PERMITS_SOURCE,
                       COUNTY_PERMITS_TABLE_ID, COUNTY_PERMITS_NOTE)


def county_permits(site_address: Optional[str], client: httpx.Client) -> list[Observation]:
    field = "permits"
    want = normalize_address(site_address or "")
    if not want["number"] or not want["name"]:
        return [Observation(field=field, status="not_provided", source=COUNTY_PERMITS_SOURCE,
                            url=COUNTY_PERMITS_URL,
                            note=f"no street address to search ({site_address!r})")]
    query = " ".join(p for p in (want["number"], want["predir"], want["name"]) if p)
    url = str(httpx.URL(COUNTY_PERMITS_URL, params={"QueryText": query}))
    common = dict(source=COUNTY_PERMITS_SOURCE, url=url, record_url=url)
    try:
        r = client.get(url, headers=HEADERS, follow_redirects=True)
    except httpx.HTTPError as e:
        return [Observation(field=field, status="source_unavailable", **common,
                            note=f"permit search: {type(e).__name__}: {e}")]
    if r.status_code != 200 or "Login.aspx" in str(r.url):
        return [Observation(field=field, status="source_unavailable", **common,
                            note=f"permit search: HTTP {r.status_code} at {r.url}")]
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find(id=COUNTY_PERMITS_TABLE_ID)
    if table is None:
        if re.search(r"returned no results|no records", soup.get_text(" "), re.I):
            return [Observation(field=field, status="not_found", **common,
                                note=f"no permit record found for {query!r}")]
        return [Observation(field=field, status="source_unavailable", **common,
                            note="permit search: no result table (layout change?)")]
    items = []
    for tr in table.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(tds) >= 5 and re.match(r"\d{1,2}/\d{1,2}/\d{4}", tds[0]):
            items.append({"permit_no": tds[1], "opened": iso_date(tds[0]),
                          "project_name": clean(tds[2]), "type": clean(tds[3]),
                          "status": clean(tds[4])})
    count = re.search(r"Showing\s+\d+-(\d+)\s+of\s+(\d+)", table.get_text(" "))
    extra = f"searched {query!r}"
    if count and int(count[2]) > len(items):
        extra += f"; only the first {len(items)} of {count[2]} records were read"
    if not items:
        return [Observation(field=field, status="not_found", **common,
                            note=f"no permit record found ({extra})")]
    items.sort(key=lambda p: p["opened"] or "", reverse=True)
    return [Observation(field=field, value=items, note=f"{COUNTY_PERMITS_NOTE}. {extra}",
                        **common)]
