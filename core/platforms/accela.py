"""Accela Citizen Access (ACA) permit portals: aca-prod.accela.com/<agency>.

Module searches and record pages often need a login; the site-wide search
(Cap/GlobalSearchResults.aspx) does not. It is searched for
"<number> <direction> <street>" and its record list read: date, record number,
project name, type, status. Config (`permits`): agency, source; base if the
agency hosts ACA elsewhere.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from core.context import Context
from core.platforms.web import HEADERS
from core.models import Observation
from core.common import clean, iso_date, normalize_address

BASE = "https://aca-prod.accela.com"
TABLE_ID = "ctl00_PlaceHolderMain_CapView_gdvPermitList"
NOTE = ("records the portal's site-wide search finds for the site address, newest first. The "
        "list does not show each record's address, and the record pages (with contacts such as "
        "the contractor) need a login")


def run(cfg: dict, ctx: Context) -> None:
    ctx.add(permits(cfg, ctx.value("site_address"), ctx.client))


def permits(cfg: dict, site_address: Optional[str], client: httpx.Client) -> list[Observation]:
    field, source = "permits", cfg["source"]
    search = f"{cfg.get('base', BASE)}/{cfg['agency']}/Cap/GlobalSearchResults.aspx"
    want = normalize_address(site_address or "")
    if not want["number"] or not want["name"]:
        return [Observation(field=field, status="not_provided", source=source, url=search,
                            note=f"no street address to search ({site_address!r})")]
    query = " ".join(p for p in (want["number"], want["predir"], want["name"]) if p)
    url = str(httpx.URL(search, params={"QueryText": query}))
    common = dict(source=source, url=url, record_url=url)
    try:
        r = client.get(url, headers=HEADERS, follow_redirects=True)
    except httpx.HTTPError as e:
        return [Observation(field=field, status="source_unavailable", **common,
                            note=f"permit search: {type(e).__name__}: {e}")]
    if r.status_code != 200 or "Login.aspx" in str(r.url):
        return [Observation(field=field, status="source_unavailable", **common,
                            note=f"permit search: HTTP {r.status_code} at {r.url}")]
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find(id=TABLE_ID)
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
    return [Observation(field=field, value=items, note=f"{NOTE}. {extra}", **common)]
